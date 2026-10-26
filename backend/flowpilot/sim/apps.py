"""Application traffic models for the simulator.

Each model generates packets with the statistical signature of the real application and
measures the application-level outcome the user actually perceives:

  GamingApp     UDP game-state exchange at a server tick rate; in-game ping, jitter, loss
  VideoCallApp  bidirectional RTP-like audio + video with GCC-style rate adaptation;
                audio MOS (E-model), frame freezes, sent/received video bitrate
  StreamingApp  DASH/HLS player over TCP with a Netflix-like bitrate ladder and a
                buffer-aware throughput ABR; bitrate, stalls, startup delay, P.1203-style MOS
  WebApp        page loads = handshake + many objects over up to 6 short TCP connections;
                page-load time (PLT)
  BulkApp       game update / OS download: N parallel long-lived TCP connections
  UploadApp     cloud photo/video backup: N parallel uploading TCP connections

Parameters are randomised by `randomize=True` so that the classifier is trained on a wide
family of applications rather than one fixed generator.
"""
from __future__ import annotations

import math
import random
from typing import Optional

from ..common import metrics as M
from ..common.classes import App
from .net import K_UDP, UDP_HDR, Flow, Packet
from .tcp import MSS, TcpConn


class OwdTracker:
    """One-way delay, loss and jitter of a sequenced packet stream."""

    __slots__ = ("int_samples", "all_samples", "recv", "max_seq", "jit", "_int_exp0", "_int_recv0",
                 "lost_total", "exp_total")

    def __init__(self) -> None:
        self.int_samples: list[float] = []
        self.all_samples: list[float] = []
        self.recv = 0
        self.max_seq = -1
        self.jit = M.Rfc3550Jitter()
        self._int_exp0 = 0
        self._int_recv0 = 0

    def on_packet(self, seq: int, send_ts: float, now: float) -> float:
        owd = (now - send_ts) * 1000.0
        self.int_samples.append(owd)
        self.all_samples.append(owd)
        self.recv += 1
        if seq > self.max_seq:
            self.max_seq = seq
        self.jit.update(send_ts, now)
        return owd

    def interval(self) -> dict:
        exp = self.max_seq + 1
        d_exp = exp - self._int_exp0
        d_recv = self.recv - self._int_recv0
        loss = max(0.0, (d_exp - d_recv) / d_exp * 100.0) if d_exp > 0 else 0.0
        out = M.summarize_latency(self.int_samples)
        out["loss_pct"] = loss
        out["jitter_ms"] = self.jit.j * 1000.0
        self.int_samples = []
        self._int_exp0 = exp
        self._int_recv0 = self.recv
        return out

    def total_loss_pct(self) -> float:
        exp = self.max_seq + 1
        return max(0.0, (exp - self.recv) / exp * 100.0) if exp > 0 else 0.0


class SimApp:
    app_type = "app"

    def __init__(self, world, app_id: str, device: str, server: str, rng: random.Random) -> None:
        self.w = world
        self.sim = world.sim
        self.app_id = app_id
        self.device = device
        self.server = server
        self.rng = rng
        self.flows: list[Flow] = []
        self.active = False
        self.t_start = 0.0
        self.t_stop: Optional[float] = None
        self.label = ""
        self.down_bytes = 0
        self.up_bytes = 0
        self._int_down = 0
        self._int_up = 0
        self.base_rtt_ms = 0.0

    def new_flow(self, proto: str, wan_port: int, lan_port: Optional[int] = None) -> Flow:
        f = self.w.new_flow(proto, self.device, self.server, wan_port, lan_port, self.app_type, self.app_id)
        self.flows.append(f)
        self.base_rtt_ms = self.w.base_rtt_ms(f)
        return f

    def start(self) -> None:
        self.active = True
        self.t_start = self.sim.now

    def stop(self) -> None:
        self.active = False
        self.t_stop = self.sim.now

    def _rate_snapshot(self, dt: float) -> dict:
        d = (self.down_bytes - self._int_down) * 8 / 1000 / dt
        u = (self.up_bytes - self._int_up) * 8 / 1000 / dt
        self._int_down = self.down_bytes
        self._int_up = self.up_bytes
        return {"kbps_down": d, "kbps_up": u}

    def snapshot(self, now: float, dt: float) -> dict:
        out = {"app_id": self.app_id, "app_type": self.app_type, "device": self.device,
               "label": self.label, "active": self.active}
        out.update(self._rate_snapshot(dt))
        return out

    def summary(self) -> dict:
        return {"app_id": self.app_id, "app_type": self.app_type, "device": self.device,
                "label": self.label}


# =========================================================================== gaming
class GamingApp(SimApp):
    app_type = App.GAMING

    def __init__(self, world, app_id, device, server, rng, tick_up=64.0, tick_down=64.0,
                 size_up=(70, 15), size_down=(300, 120), port=27015, randomize=False, title="CS2") -> None:
        super().__init__(world, app_id, device, server, rng)
        if randomize:
            tick_up = rng.choice([20, 30, 32, 60, 64, 64, 120, 128])
            tick_down = tick_up if rng.random() < 0.7 else rng.choice([20, 30, 60, 64])
            size_up = (rng.uniform(40, 160), rng.uniform(5, 40))
            size_down = (rng.uniform(80, 700), rng.uniform(20, 250))
        self.tick_up, self.tick_down = tick_up, tick_down
        self.size_up, self.size_down = size_up, size_down
        self.label = f"{title} on {device}"
        self.flow = self.new_flow("udp", port)
        self.flow.recv_up = self._server_rx
        self.flow.recv_down = self._client_rx
        self.up = OwdTracker()
        self.down = OwdTracker()
        self.rtt_int: list[float] = []
        self.rtt_all: list[float] = []
        self._seq_up = 0
        self._seq_down = 0
        self._last_client_ts: Optional[float] = None
        self._last_client_rx = 0.0

    def start(self) -> None:
        super().start()
        self.sim.after(self.rng.random() / self.tick_up, self._client_tick)
        self.sim.after(self.rng.random() / self.tick_down, self._server_tick)

    def _size(self, mu_sigma, lo, hi) -> int:
        return int(min(hi, max(lo, self.rng.gauss(*mu_sigma)))) + UDP_HDR

    def _client_tick(self) -> None:
        if not self.active:
            return
        sz = self._size(self.size_up, 20, 500)
        p = Packet(self.flow, sz, K_UDP, True, self.sim.now, seq=self._seq_up)
        self._seq_up += 1
        self.up_bytes += sz
        self.w.send_up(p)
        self.sim.after((1.0 / self.tick_up) * self.rng.uniform(0.97, 1.03), self._client_tick)

    def _server_rx(self, pkt: Packet) -> None:
        now = self.sim.now
        self.up.on_packet(pkt.seq, pkt.ts, now)
        self._last_client_ts = pkt.ts
        self._last_client_rx = now

    def _server_tick(self) -> None:
        if not self.active:
            return
        now = self.sim.now
        sz = self._size(self.size_down, 40, 1400)
        if self.rng.random() < 0.01:
            sz = min(1400, sz * 4)  # occasional burst (explosions, spawns)
        echo = None
        if self._last_client_ts is not None:
            echo = (self._last_client_ts, now - self._last_client_rx)
        p = Packet(self.flow, sz, K_UDP, False, now, seq=self._seq_down, payload=echo)
        self._seq_down += 1
        self.w.send_down(p)
        self.sim.after((1.0 / self.tick_down) * self.rng.uniform(0.99, 1.01), self._server_tick)

    def _client_rx(self, pkt: Packet) -> None:
        now = self.sim.now
        self.down_bytes += pkt.size
        self.down.on_packet(pkt.seq, pkt.ts, now)
        if pkt.payload is not None:
            ts, hold = pkt.payload
            rtt = (now - ts - hold) * 1000.0
            self.rtt_int.append(rtt)
            self.rtt_all.append(rtt)

    def snapshot(self, now, dt) -> dict:
        out = super().snapshot(now, dt)
        up = self.up.interval()
        down = self.down.interval()
        rtt = M.summarize_latency(self.rtt_int)
        self.rtt_int = []
        loss = (up["loss_pct"] + down["loss_pct"]) / 2
        jitter = max(up["jitter_ms"], down["jitter_ms"])
        out.update(rtt_ms=rtt, latency_ms=rtt["p50"], latency_p95_ms=rtt["p95"], latency_p99_ms=rtt["p99"],
                   jitter_ms=jitter, loss_pct=loss, base_rtt_ms=self.base_rtt_ms,
                   mos=M.gaming_mos(rtt["p95"], jitter, loss) if rtt["n"] else None)
        return out

    def summary(self) -> dict:
        s = super().summary()
        rtt = M.summarize_latency(self.rtt_all)
        loss = (self.up.total_loss_pct() + self.down.total_loss_pct()) / 2
        r = self.rtt_all
        jitter = sum(abs(r[i + 1] - r[i]) for i in range(len(r) - 1)) / (len(r) - 1) if len(r) > 2 else 0.0
        s.update(rtt_ms=rtt, loss_pct=loss, jitter_ms=jitter, base_rtt_ms=self.base_rtt_ms,
                 added_latency_p95_ms=(rtt["p95"] - self.base_rtt_ms) if rtt["n"] else None,
                 added_latency_p99_ms=(rtt["p99"] - self.base_rtt_ms) if rtt["n"] else None,
                 mos=M.gaming_mos(rtt["p95"], jitter, loss) if rtt["n"] else None)
        return s


# =========================================================================== video call
class _RtcSender:
    """One media direction of a call (audio + adaptive video), GCC-like congestion control."""

    def __init__(self, app: "VideoCallApp", up: bool, max_kbps: float, min_kbps: float = 300.0,
                 start_kbps: float = 1200.0, fps: float = 30.0) -> None:
        self.app = app
        self.up = up
        self.rate = start_kbps
        self.max_kbps = max_kbps
        self.min_kbps = min_kbps
        self.fps = fps
        self.seq_v = 0
        self.seq_a = 0
        self.frame_no = 0
        self.rates: list[float] = []

    def on_report(self, loss: float, qdelay_ms: float, trend_ms: float) -> None:
        r = self.rate
        if loss > 0.10:
            r *= 1 - 0.5 * loss
        elif qdelay_ms > 60 or (qdelay_ms > 25 and trend_ms > 2):
            r *= 0.90  # delay-based over-use
        elif loss < 0.02 and qdelay_ms < 15:
            r = r * 1.04 + 8
        self.rate = min(self.max_kbps, max(self.min_kbps, r))


class _RtcReceiver:
    def __init__(self) -> None:
        self.audio = OwdTracker()
        self.video_owd = OwdTracker()
        self.frames: dict[int, list] = {}  # frame -> [npkts_expected, npkts_got, t_capture, t_last]
        self.frames_done = 0
        self.frames_late = 0
        self.frames_total = 0
        self.int_frames = [0, 0]  # total, frozen (late or incomplete)
        self.rep_owd_sum = 0.0
        self.rep_n = 0
        self.rep_exp0 = 0
        self.rep_recv0 = 0
        self.min_owd = 1e9
        self.last_q = 0.0
        self.video_bytes = 0


class VideoCallApp(SimApp):
    app_type = App.VIDEO_CALL
    FREEZE_MS = 250.0  # a frame later than this behind capture is perceived as a freeze

    def __init__(self, world, app_id, device, server, rng, max_down_kbps=2500.0, max_up_kbps=3000.0,
                 port=3478, randomize=False, title="Teams call") -> None:
        super().__init__(world, app_id, device, server, rng)
        fps = 30.0
        if randomize:
            max_down_kbps = rng.uniform(600, 4000)
            max_up_kbps = rng.uniform(500, 3500)
            fps = rng.choice([15.0, 24.0, 30.0, 30.0])
        self.label = f"{title} on {device}"
        self.flow = self.new_flow("udp", port)
        self.flow.recv_up = self._rx_at_peer
        self.flow.recv_down = self._rx_at_client
        self.tx = {True: _RtcSender(self, True, max_up_kbps, fps=fps),
                   False: _RtcSender(self, False, max_down_kbps, fps=fps)}
        self.rx = {True: _RtcReceiver(), False: _RtcReceiver()}  # keyed by direction of media

    def start(self) -> None:
        super().start()
        for up in (True, False):
            self.sim.after(self.rng.random() * 0.02, self._audio_tick, up)
            self.sim.after(self.rng.random() / 30.0, self._video_tick, up)
            self.sim.after(0.1 + self.rng.random() * 0.1, self._report_tick, up)

    def _send(self, up: bool, size: int, payload) -> None:
        p = Packet(self.flow, size, K_UDP, up, self.sim.now, payload=payload)
        if up:
            self.up_bytes += size
        self.w.send_up(p) if up else self.w.send_down(p)

    def _audio_tick(self, up: bool) -> None:
        if not self.active:
            return
        tx = self.tx[up]
        self._send(up, 200, ("a", tx.seq_a, 0, 0))
        tx.seq_a += 1
        self.sim.after(0.020, self._audio_tick, up)

    def _video_tick(self, up: bool) -> None:
        if not self.active:
            return
        tx = self.tx[up]
        key = tx.frame_no % 60 == 0
        avg = tx.rate * 1000 / 8 / tx.fps
        size = avg * (3.0 if key else 0.96) * math.exp(self.rng.gauss(0, 0.15))
        n = max(1, int(math.ceil(size / 1150)))
        per = int(size / n) + 40
        gap = per * 8 / (2.5 * tx.rate * 1000)  # WebRTC pacer: 2.5x target bitrate
        for i in range(n):
            self.sim.after(i * gap, self._send, up, per, ("v", tx.seq_v + i, tx.frame_no, n))
        tx.seq_v += n
        tx.frame_no += 1
        tx.rates.append(tx.rate)
        self.sim.after(1.0 / tx.fps, self._video_tick, up)

    def _rx(self, pkt: Packet, up: bool) -> None:
        now = self.sim.now
        rx = self.rx[up]
        kind, seq, frame, n = pkt.payload
        if kind == "a":
            owd = rx.audio.on_packet(seq, pkt.ts, now)
            rx.rep_owd_sum += owd
            rx.rep_n += 1
            if owd < rx.min_owd:
                rx.min_owd = owd
        elif kind == "v":
            rx.video_owd.on_packet(seq, pkt.ts, now)
            rx.video_bytes += pkt.size
            st = rx.frames.get(frame)
            if st is None:
                st = rx.frames[frame] = [n, 0, pkt.ts, now]
            st[1] += 1
            st[3] = now
        elif kind == "r":
            self.tx[not up].on_report(*pkt.payload[1])
        if not up:
            self.down_bytes += pkt.size

    def _rx_at_peer(self, pkt: Packet) -> None:
        self._rx(pkt, True)

    def _rx_at_client(self, pkt: Packet) -> None:
        self._rx(pkt, False)

    def _report_tick(self, up: bool) -> None:
        """Receiver of direction `up` reports loss/delay back to that direction's sender."""
        if not self.active:
            return
        rx = self.rx[up]
        exp = rx.audio.max_seq + 1
        d_exp = exp - rx.rep_exp0
        d_recv = rx.audio.recv - rx.rep_recv0
        loss = max(0.0, (d_exp - d_recv) / d_exp) if d_exp > 0 else 0.0
        q = (rx.rep_owd_sum / rx.rep_n - rx.min_owd) if rx.rep_n else rx.last_q
        trend = q - rx.last_q
        rx.last_q = q
        rx.rep_exp0, rx.rep_recv0 = exp, rx.audio.recv
        rx.rep_owd_sum, rx.rep_n = 0.0, 0
        self._frames_close(rx)
        # feedback travels on the reverse direction (RTCP); a lost report is simply skipped
        p = Packet(self.flow, 80, K_UDP, not up, self.sim.now, payload=("r", (loss, q, trend), 0, 0))
        self.w.send_up(p) if (not up) else self.w.send_down(p)
        self.sim.after(0.1, self._report_tick, up)

    def _frames_close(self, rx: _RtcReceiver) -> None:
        now = self.sim.now
        done = [f for f, st in rx.frames.items() if now - st[2] > 0.5]
        for f in done:
            n, got, t_cap, t_last = rx.frames.pop(f)
            rx.frames_total += 1
            rx.int_frames[0] += 1
            frozen = got < n or (t_last - t_cap) * 1000 > self.FREEZE_MS
            if frozen:
                rx.frames_late += 1
                rx.int_frames[1] += 1

    def _dir_snapshot(self, up: bool) -> dict:
        rx = self.rx[up]
        a = rx.audio.interval()
        rx.video_owd.interval()
        tot, frz = rx.int_frames
        rx.int_frames = [0, 0]
        return {"owd": a, "freeze_pct": (frz / tot * 100) if tot else 0.0, "rate_kbps": self.tx[up].rate}

    def snapshot(self, now, dt) -> dict:
        out = super().snapshot(now, dt)
        down_samples = list(self.rx[False].audio.int_samples)
        up_samples = list(self.rx[True].audio.int_samples)
        dn = self._dir_snapshot(False)
        upd = self._dir_snapshot(True)
        mos_d = M.voice_mos(down_samples, dn["owd"]["loss_pct"], dn["owd"]["jitter_ms"])
        mos_u = M.voice_mos(up_samples, upd["owd"]["loss_pct"], upd["owd"]["jitter_ms"])
        mos_vals = [m["mos"] for m in (mos_d, mos_u) if m["mos"] is not None]
        mos = min(mos_vals) if mos_vals else None
        freeze = max(dn["freeze_pct"], upd["freeze_pct"])
        if mos is not None:
            mos = max(1.0, mos - 0.02 * freeze)
        lat = [x for x in (dn["owd"]["p50"], upd["owd"]["p50"]) if x is not None]
        lat95 = [x for x in (dn["owd"]["p95"], upd["owd"]["p95"]) if x is not None]
        lat99 = [x for x in (dn["owd"]["p99"], upd["owd"]["p99"]) if x is not None]
        out.update(owd_down_ms=dn["owd"], owd_up_ms=upd["owd"],
                   latency_ms=max(lat) if lat else None, latency_p95_ms=max(lat95) if lat95 else None,
                   latency_p99_ms=max(lat99) if lat99 else None,
                   jitter_ms=max(dn["owd"]["jitter_ms"], upd["owd"]["jitter_ms"]),
                   loss_pct=max(dn["owd"]["loss_pct"], upd["owd"]["loss_pct"]),
                   freeze_pct=freeze, video_kbps_down=dn["rate_kbps"], video_kbps_up=upd["rate_kbps"],
                   base_owd_ms=self.base_rtt_ms / 2, mos=mos)
        return out

    def summary(self) -> dict:
        s = super().summary()
        res = {}
        mos_all = []
        for up, name in ((False, "down"), (True, "up")):
            rx = self.rx[up]
            self._frames_close(rx)
            owd = M.summarize_latency(rx.audio.all_samples)
            loss = rx.audio.total_loss_pct()
            jit = rx.audio.jit.j * 1000
            v = M.voice_mos(rx.audio.all_samples, loss, jit)
            freeze = rx.frames_late / rx.frames_total * 100 if rx.frames_total else 0.0
            rates = self.tx[up].rates
            res[name] = {"owd_ms": owd, "loss_pct": loss, "jitter_ms": jit, "voice_mos": v["mos"],
                         "freeze_pct": freeze, "avg_video_kbps": sum(rates) / len(rates) if rates else 0.0}
            if v["mos"] is not None:
                mos_all.append(max(1.0, v["mos"] - 0.02 * freeze))
        p95 = [res[d]["owd_ms"]["p95"] for d in res if res[d]["owd_ms"]["p95"] is not None]
        p99 = [res[d]["owd_ms"]["p99"] for d in res if res[d]["owd_ms"]["p99"] is not None]
        base = self.base_rtt_ms / 2
        s.update(directions=res, mos=min(mos_all) if mos_all else None,
                 loss_pct=max(res["down"]["loss_pct"], res["up"]["loss_pct"]),
                 owd_p95_ms=max(p95) if p95 else None, owd_p99_ms=max(p99) if p99 else None,
                 added_latency_p95_ms=(max(p95) - base) if p95 else None,
                 added_latency_p99_ms=(max(p99) - base) if p99 else None,
                 freeze_pct=max(res["down"]["freeze_pct"], res["up"]["freeze_pct"]),
                 video_kbps_down=res["down"]["avg_video_kbps"], video_kbps_up=res["up"]["avg_video_kbps"],
                 jitter_ms=max(res["down"]["jitter_ms"], res["up"]["jitter_ms"]))
        return s


# =========================================================================== streaming (ABR)
NETFLIX_LADDER_KBPS = [235, 375, 560, 750, 1050, 1750, 2350, 3000, 4300, 5800, 8000, 12000, 16000]


class StreamingApp(SimApp):
    app_type = App.STREAMING

    def __init__(self, world, app_id, device, server, rng, top_kbps=16000, segment_s=4.0,
                 max_buffer_s=30.0, port=443, randomize=False, title="Netflix") -> None:
        super().__init__(world, app_id, device, server, rng)
        if randomize:
            top_kbps = rng.choice([3000, 5800, 8000, 12000, 16000, 16000])
            segment_s = rng.choice([2.0, 4.0, 4.0, 6.0])
            max_buffer_s = rng.uniform(12, 60)
        self.ladder = [b for b in NETFLIX_LADDER_KBPS if b <= top_kbps]
        self.segment_s = segment_s
        self.max_buffer_s = max_buffer_s
        self.label = f"{title} on {device}"
        self.flow = self.new_flow("tcp", port)
        self.conn = TcpConn(world, self.flow, data_up=False)
        self.conn.on_ctrl_server = self._server_ctrl
        self.conn.on_ctrl_client = self._client_ctrl
        self.conn.on_delivered = self._on_bytes
        self.buffer = 0.0
        self.t_buf = 0.0
        self.playing = False
        self.started_play: Optional[float] = None
        self.stalled = False
        self.stall_start = 0.0
        self.stall_time = 0.0
        self.stalls = 0
        self.int_stall_time = 0.0
        self.level = 0
        self.tputs: list[float] = []
        self.seg_bitrates: list[float] = []
        self.switches = 0
        self.req_t = 0.0
        self.seg_idx = 0
        self._underrun_gen = 0
        self._fetching = False

    def start(self) -> None:
        super().start()
        self.t_buf = self.sim.now
        self.conn.send_ctrl(True, 64, ("syn",))

    def _server_ctrl(self, pkt: Packet) -> None:
        p = pkt.payload
        if p[0] == "syn":
            self.conn.send_ctrl(False, 64, ("synack",))
        elif p[0] == "get":
            self.conn.send_msg(p[2], on_complete=self._segment_done)

    def _client_ctrl(self, pkt: Packet) -> None:
        if pkt.payload[0] == "synack" and self.active:
            self._request_next()

    def _on_bytes(self, n: int) -> None:
        self.down_bytes += n + 52 * n // MSS

    def _advance_buffer(self) -> None:
        now = self.sim.now
        if self.playing and not self.stalled:
            self.buffer = max(0.0, self.buffer - (now - self.t_buf))
        self.t_buf = now

    def _choose_level(self) -> int:
        if not self.tputs:
            return 0
        recent = self.tputs[-3:]
        est = len(recent) / sum(1.0 / max(t, 1.0) for t in recent)  # harmonic mean, kbps
        b = self.buffer
        safety = 0.9 if b > 15 else (0.75 if b > 6 else 0.5)
        target = est * safety
        lvl = 0
        for i, br in enumerate(self.ladder):
            if br <= target:
                lvl = i
        max_up = 4 if b >= 8 else 2
        if lvl > self.level + max_up:
            lvl = self.level + max_up
        return min(lvl, len(self.ladder) - 1)

    def _request_next(self) -> None:
        if not self.active or self._fetching:
            return
        self._advance_buffer()
        new = self._choose_level()
        if self.seg_bitrates and new != self.level:
            self.switches += 1
        self.level = new
        br = self.ladder[self.level]
        nbytes = br * 1000 / 8 * self.segment_s * self.rng.uniform(0.9, 1.1)
        self.req_t = self.sim.now
        self._fetching = True
        self.conn.send_ctrl(True, 420, ("get", self.seg_idx, nbytes))
        self.seg_idx += 1
        self._cur_bytes = nbytes

    def _segment_done(self) -> None:
        now = self.sim.now
        self._fetching = False
        self._advance_buffer()
        dt = max(1e-3, now - self.req_t)
        self.tputs.append(self._cur_bytes * 8 / 1000 / dt)
        self.seg_bitrates.append(self.ladder[self.level] / 1000.0)
        self.buffer += self.segment_s
        if self.stalled and self.buffer >= self.segment_s:
            self.stalled = False
            st = now - self.stall_start
            self.stall_time += st
            self.int_stall_time += st
        if not self.playing and self.buffer >= self.segment_s:
            self.playing = True
            self.started_play = now
        self._arm_underrun()
        if not self.active:
            return
        if self.buffer + self.segment_s <= self.max_buffer_s:
            self._request_next()
        else:
            wait = self.buffer + self.segment_s - self.max_buffer_s
            self.sim.after(wait, self._request_next)

    def _arm_underrun(self) -> None:
        self._underrun_gen += 1
        if self.playing and not self.stalled:
            self.sim.after(self.buffer, self._underrun, self._underrun_gen)

    def _underrun(self, gen: int) -> None:
        if gen != self._underrun_gen or not self.active:
            return
        self._advance_buffer()
        if self.buffer <= 1e-6 and self.playing and not self.stalled:
            self.stalled = True
            self.stall_start = self.sim.now
            self.stalls += 1

    def stop(self) -> None:
        if self.stalled:
            st = self.sim.now - self.stall_start
            self.stall_time += st
            self.int_stall_time += st
            self.stalled = False
        super().stop()
        self.conn.close()

    def snapshot(self, now, dt) -> dict:
        out = super().snapshot(now, dt)
        self._advance_buffer()
        stall = self.int_stall_time
        if self.stalled:
            stall += now - max(self.stall_start, now - dt)
            self.stall_start = max(self.stall_start, now)  # avoid double counting
            self.stall_time += stall - self.int_stall_time
        self.int_stall_time = 0.0
        br = self.ladder[self.level] / 1000 if self.seg_bitrates else None
        out.update(bitrate_mbps=br, buffer_s=self.buffer, stalled=self.stalled, rebuffer_s=stall,
                   startup_s=(self.started_play - self.t_start) if self.started_play else None,
                   mos=M.streaming_mos(br, stall / dt if dt else 0.0, 0.0) if br is not None else None)
        return out

    def summary(self) -> dict:
        s = super().summary()
        end = self.t_stop if self.t_stop is not None else self.sim.now
        play_span = max(1e-6, end - (self.started_play or end))
        watch = max(1e-6, end - self.t_start)
        avg = sum(self.seg_bitrates) / len(self.seg_bitrates) if self.seg_bitrates else None
        stall_time = self.stall_time + (self.sim.now - self.stall_start if self.stalled else 0.0)
        rr = stall_time / (play_span + stall_time) if self.started_play else 1.0
        mins = watch / 60
        s.update(avg_bitrate_mbps=avg, rebuffer_ratio=rr, rebuffer_s=stall_time, stalls=self.stalls,
                 startup_s=(self.started_play - self.t_start) if self.started_play else None,
                 switches=self.switches, segments=len(self.seg_bitrates),
                 qoe_lin=M.qoe_lin(self.seg_bitrates, stall_time),
                 mos=M.streaming_mos(avg, rr, self.stalls / mins, self.switches / mins) if avg else 1.0)
        return s


# =========================================================================== web
class WebApp(SimApp):
    app_type = App.WEB

    def __init__(self, world, app_id, device, server, rng, think_s=6.0, randomize=False, title="Browsing") -> None:
        super().__init__(world, app_id, device, server, rng)
        if randomize:
            think_s = rng.uniform(2, 15)
        self.think_s = think_s
        self.label = f"{title} on {device}"
        self.plt_int: list[float] = []
        self.plt_all: list[float] = []
        self._page_id = 0

    def start(self) -> None:
        super().start()
        self.sim.after(self.rng.uniform(0.2, 2.0), self._page)

    def _page(self) -> None:
        if not self.active:
            return
        rng = self.rng
        objs = [min(2e6, rng.lognormvariate(math.log(18e3), 1.1)) for _ in range(rng.randint(8, 30))]
        main = min(1e6, rng.lognormvariate(math.log(60e3), 0.6))
        page = {"t0": self.sim.now, "pending": len(objs) + 1, "objs": objs, "id": self._page_id}
        self._page_id += 1
        nconn = min(6, 1 + len(objs) // 4)
        conns = [self._open(page, first=(i == 0), main=main) for i in range(nconn)]
        page["conns"] = conns

    def _open(self, page: dict, first: bool, main: float) -> dict:
        flow = self.new_flow("tcp", 443)
        conn = TcpConn(self.w, flow, data_up=False)
        st = {"conn": conn, "flow": flow, "page": page, "first": first, "main": main, "phase": 0}

        def on_server(pkt, st=st):
            kind = pkt.payload[0]
            if kind == "syn":
                st["conn"].send_ctrl(False, 64, ("synack",))
            elif kind == "hello":  # TLS 1.3: server hello + certificate chain
                st["conn"].send_msg(4200, on_complete=lambda st=st: self._tls_done(st))
            elif kind == "get":
                st["conn"].send_msg(pkt.payload[1], on_complete=lambda st=st: self._obj_done(st))

        def on_client(pkt, st=st):
            if pkt.payload[0] == "synack":
                st["conn"].send_ctrl(True, 320, ("hello",))

        conn.on_ctrl_server = on_server
        conn.on_ctrl_client = on_client
        conn.on_delivered = self._on_bytes
        conn.send_ctrl(True, 64, ("syn",))
        if not first:
            # secondary connections wait until the main document has been parsed
            st["wait_main"] = True
        return st

    def _on_bytes(self, n: int) -> None:
        self.down_bytes += n + 52 * n // MSS

    def _tls_done(self, st: dict) -> None:
        st["phase"] = 1
        self._next_obj(st)

    def _next_obj(self, st: dict) -> None:
        page = st["page"]
        if st["first"] and not page.get("main_sent"):
            page["main_sent"] = True
            st["conn"].send_ctrl(True, 420, ("get", st["main"]))
            return
        if not page.get("main_done"):
            st["idle"] = True
            return
        if page["objs"]:
            st["conn"].send_ctrl(True, 420, ("get", page["objs"].pop()))
        else:
            st["conn"].close()

    def _obj_done(self, st: dict) -> None:
        page = st["page"]
        page["pending"] -= 1
        if st["first"] and not page.get("main_done"):
            page["main_done"] = True
            for other in page["conns"]:
                if other is not st and other.get("idle"):
                    other["idle"] = False
                    self._next_obj(other)
        if page["pending"] <= 0:
            plt = self.sim.now - page["t0"]
            self.plt_int.append(plt)
            self.plt_all.append(plt)
            for c in page["conns"]:
                c["conn"].close()
            self.sim.after(max(1.0, self.rng.expovariate(1.0 / self.think_s)), self._page)
            return
        self._next_obj(st)

    def snapshot(self, now, dt) -> dict:
        out = super().snapshot(now, dt)
        plt = M.pct(self.plt_int, 50) if self.plt_int else None
        self.plt_int = []
        out.update(plt_s=plt, mos=M.web_mos(plt) if plt is not None else None)
        return out

    def summary(self) -> dict:
        s = super().summary()
        p50 = M.pct(self.plt_all, 50) if self.plt_all else None
        p95 = M.pct(self.plt_all, 95) if self.plt_all else None
        s.update(pages=len(self.plt_all), plt_p50_s=p50, plt_p95_s=p95, mos=M.web_mos(p50) if p50 else 1.0)
        return s


# =========================================================================== bulk
class BulkApp(SimApp):
    """Long-lived TCP transfers. `upload=True` models cloud backup on the uplink."""

    app_type = App.BULK

    def __init__(self, world, app_id, device, server, rng, n_flows=4, upload=False, randomize=False,
                 title=None) -> None:
        super().__init__(world, app_id, device, server, rng)
        if randomize:
            n_flows = rng.choice([1, 2, 4, 4, 6, 8, 16])
        self.upload = upload
        if upload:
            self.app_type = App.UPLOAD
        self.n_flows = n_flows
        self.label = f"{title or ('Cloud backup' if upload else 'Game update')} on {device}"
        self.conns: list[TcpConn] = []
        self.tput_int: list[float] = []

    def start(self) -> None:
        super().start()
        for i in range(self.n_flows):
            self.sim.after(0.15 * i + self.rng.random() * 0.05, self._open_one)

    def _open_one(self) -> None:
        if not self.active:
            return
        f = self.new_flow("tcp", 443)
        c = TcpConn(self.w, f, data_up=self.upload)
        c.on_delivered = self._on_bytes

        def on_client(pkt, c=c):
            if pkt.payload[0] == "synack":
                if self.upload:
                    c.send_msg(math.inf)
                else:
                    c.send_ctrl(True, 420, ("get",))

        def on_server(pkt, c=c):
            if pkt.payload[0] == "syn":
                c.send_ctrl(False, 64, ("synack",))
            elif pkt.payload[0] == "get":
                c.send_msg(math.inf)

        c.on_ctrl_client = on_client
        c.on_ctrl_server = on_server
        c.send_ctrl(True, 64, ("syn",))
        self.conns.append(c)

    def _on_bytes(self, n: int) -> None:
        if self.upload:
            self.up_bytes += n + 52 * n // MSS
        else:
            self.down_bytes += n + 52 * n // MSS

    def stop(self) -> None:
        super().stop()
        for c in self.conns:
            c.close()

    def snapshot(self, now, dt) -> dict:
        out = super().snapshot(now, dt)
        mbps = (out["kbps_up"] if self.upload else out["kbps_down"]) / 1000
        if self.active:
            self.tput_int.append(mbps)
        out.update(throughput_mbps=mbps, flows=len(self.conns))
        return out

    def summary(self) -> dict:
        s = super().summary()
        end = self.t_stop if self.t_stop is not None else self.sim.now
        span = max(1e-6, end - self.t_start)
        tot = self.up_bytes if self.upload else self.down_bytes
        s.update(avg_throughput_mbps=tot * 8 / 1e6 / span, flows=self.n_flows, upload=self.upload)
        return s


APP_CLASSES = {
    App.GAMING: GamingApp,
    App.VIDEO_CALL: VideoCallApp,
    App.STREAMING: StreamingApp,
    App.WEB: WebApp,
    App.BULK: BulkApp,
    App.UPLOAD: BulkApp,
}
