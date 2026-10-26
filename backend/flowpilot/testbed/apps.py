"""Live traffic generators for the Linux testbed (real sockets, real kernel TCP/UDP).

Each class mirrors its simulator counterpart in `flowpilot.sim.apps` - same packet
patterns, same adaptation logic (GCC-like call rate control, buffer-aware ABR) and the
*same metric keys*, so dashboards and summaries treat both backends identically.

All sockets are created inside the right namespace (device namespace for the client,
`fp-inet` for the server) and serviced by one asyncio event loop. Every namespace shares
the host's CLOCK_MONOTONIC, so one-way delays are measured exactly.

Base path delay: the Internet path delay beyond the ISP (e.g. 12 ms to the game server)
is applied for real by the TUN delay line (or netem); without either it is added
analytically to the measured delays (`extra_owd_ms`), which leaves all queueing effects -
the thing under test - fully real. `base_owd_ms` is the base path delay itself, however it
is applied: "added latency" and the SLOs are measured against it, as in the simulator.
"""
from __future__ import annotations

import asyncio
import math
import random
import socket
import struct
import time
from typing import Callable, Optional

from ..common import metrics as M
from ..common.classes import App
from ..sim.apps import NETFLIX_LADDER_KBPS, OwdTracker
from .netns import ns_socket

HDR = struct.Struct("!HBBIIqqq")  # magic, kind, flags, seq, frame/npkts, t_send_ns, echo_ns, hold_ns
MAGIC = 0xF10F
K_GAME, K_AUDIO, K_VIDEO, K_REPORT = 1, 2, 3, 4
WIRE = 1500 / 1448  # payload -> on-the-wire bytes for TCP accounting
TCP_CONGESTION = getattr(socket, "TCP_CONGESTION", 13)
PAD = bytes(1500)
BUF = bytes(256 * 1024)


def mono() -> float:
    return time.monotonic()


def wall() -> float:
    return time.time()


def wall_ns() -> int:
    # CLOCK_REALTIME: shared by all namespaces and the clock of kernel RX timestamps
    return time.time_ns()


SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
_TS = struct.Struct("qq")


def set_cc(sock: socket.socket, cc: str = "cubic") -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, cc.encode())
    except OSError:
        pass


class UdpEndpoint:
    """Non-blocking UDP socket whose receive callback gets the *kernel* arrival time.

    Timestamps come from SO_TIMESTAMPNS, taken when the packet reaches the socket, so a
    busy event loop can delay processing but never the measured one-way delay."""

    def __init__(self, sock: socket.socket, cb: Callable[[bytes, tuple, int], None]) -> None:
        self.sock = sock
        self.cb = cb
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(sock.fileno(), self._readable)

    def _readable(self) -> None:
        sock = self.sock
        for _ in range(256):
            try:
                data, anc, _flags, addr = sock.recvmsg(2048, 64)
            except BlockingIOError:
                return
            except OSError:
                continue  # e.g. ICMP port unreachable reported on a connected socket
            rx = 0
            for lvl, typ, cd in anc:
                if lvl == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS and len(cd) >= 16:
                    sec, nsec = _TS.unpack_from(cd)
                    rx = sec * 1_000_000_000 + nsec
            self.cb(data, addr, rx or wall_ns())

    def sendto(self, data: bytes, addr=None) -> None:
        try:
            if addr is None:
                self.sock.send(data)
            else:
                self.sock.sendto(data, addr)
        except (BlockingIOError, OSError):
            pass  # socket buffer full: the packet is lost, as on a real host

    def close(self) -> None:
        try:
            self.loop.remove_reader(self.sock.fileno())
        except (ValueError, OSError):
            pass


async def udp(sock: socket.socket, cb) -> UdpEndpoint:
    return UdpEndpoint(sock, cb)


async def ticker(period_fn: Callable[[], float], fn: Callable[[], None], alive: Callable[[], bool]) -> None:
    nxt = mono()
    while alive():
        fn()
        nxt += period_fn()
        d = nxt - mono()
        if d > 0:
            await asyncio.sleep(d)
        else:
            nxt = mono()
            await asyncio.sleep(0)


class Env:
    """What an app needs to know about the testbed."""

    def __init__(self, dev_ns: Callable[[str], str], dev_ip: Callable[[str], str], srv_ip: Callable[[str], str],
                 extra_owd_ms: Callable[[str], float], inet_ns: str = "fp-inet",
                 on_flow: Optional[Callable[[tuple, str, str], None]] = None,
                 base_owd_ms: Optional[Callable[[str], float]] = None) -> None:
        self.dev_ns = dev_ns
        self.dev_ip = dev_ip
        self.srv_ip = srv_ip
        self.extra_owd_ms = extra_owd_ms
        self.base_owd_ms = base_owd_ms or extra_owd_ms
        self.inet_ns = inet_ns
        self.on_flow = on_flow or (lambda key, app_type, app_id: None)


class LiveApp:
    app_type = "app"

    def __init__(self, env: Env, app_id: str, device: str, server: str, rng: random.Random, **params) -> None:
        self.env = env
        self.app_id = app_id
        self.device = device
        self.server = server
        self.rng = rng
        self.params = params
        self.active = False
        self.t_start = 0.0
        self.t_stop: Optional[float] = None
        self.label = f"{params.get('title', self.app_type)} on {device}"
        self.down_bytes = 0
        self.up_bytes = 0
        self._int_down = 0
        self._int_up = 0
        self.tasks: list[asyncio.Task] = []
        self.socks: list[socket.socket] = []
        self.endpoints: list[UdpEndpoint] = []
        self.extra = env.extra_owd_ms(server)  # added analytically to measurements (0 with a delay line)
        self.base_owd = env.base_owd_ms(server)  # the base path delay, however it is applied
        self.base_rtt_ms = 2 * self.base_owd

    def register(self, proto: str, sock: socket.socket) -> None:
        lip, lport = sock.getsockname()[:2]
        rip, rport = sock.getpeername()[:2]
        self.env.on_flow((proto, lip, lport, rip, rport), self.app_type, self.app_id)

    def spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(t)

    async def start(self) -> None:
        self.active = True
        self.t_start = mono()

    async def stop(self) -> None:
        self.active = False
        self.t_stop = mono()
        for t in self.tasks:
            t.cancel()
        for ep in getattr(self, "endpoints", []):
            ep.close()
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass

    def _rates(self, dt: float) -> dict:
        d = (self.down_bytes - self._int_down) * 8 / 1000 / dt
        u = (self.up_bytes - self._int_up) * 8 / 1000 / dt
        self._int_down, self._int_up = self.down_bytes, self.up_bytes
        return {"kbps_down": d, "kbps_up": u}

    def snapshot(self, dt: float) -> dict:
        out = {"app_id": self.app_id, "app_type": self.app_type, "device": self.device,
               "label": self.label, "active": self.active}
        out.update(self._rates(dt))
        return out

    def summary(self) -> dict:
        return {"app_id": self.app_id, "app_type": self.app_type, "device": self.device, "label": self.label}


# ============================================================================ gaming
class LiveGaming(LiveApp):
    app_type = App.GAMING

    async def start(self) -> None:
        await super().start()
        p = self.params
        self.port = int(p.get("port", 27015))
        self.tick_up = float(p.get("tick_up", 64))
        self.tick_down = float(p.get("tick_down", 64))
        self.size_up = p.get("size_up", (70, 15))
        self.size_down = p.get("size_down", (300, 120))
        srv = self.env.srv_ip(self.server)
        self.ssock = ns_socket(self.env.inet_ns, bind=(srv, self.port))
        self.csock = ns_socket(self.env.dev_ns(self.device), bind=("0.0.0.0", 0))
        self.csock.connect((srv, self.port))
        self.socks += [self.ssock, self.csock]
        self.register("udp", self.csock)
        self.up, self.down = OwdTracker(), OwdTracker()
        self.rtt_int: list[float] = []
        self.rtt_all: list[float] = []
        self.seq_up = self.seq_down = 0
        self.last_client = None  # (t_send_ns, t_rx_ns, addr)
        self.st = await udp(self.ssock, self._srv_rx)
        self.ct = await udp(self.csock, self._cli_rx)
        self.endpoints += [self.st, self.ct]
        self.spawn(ticker(lambda: 1 / self.tick_up * self.rng.uniform(0.97, 1.03), self._cli_tx, lambda: self.active))
        self.spawn(ticker(lambda: 1 / self.tick_down, self._srv_tx, lambda: self.active))

    def _sz(self, ms, lo, hi) -> int:
        return int(min(hi, max(lo, self.rng.gauss(*ms))))

    def _cli_tx(self) -> None:
        n = self._sz(self.size_up, 36, 500)
        pkt = HDR.pack(MAGIC, K_GAME, 0, self.seq_up, 0, wall_ns(), 0, 0) + PAD[: n - HDR.size]
        self.seq_up += 1
        self.up_bytes += n + 28
        self.ct.sendto(pkt)

    def _srv_rx(self, data: bytes, addr, now: int) -> None:
        _, _, _, seq, _, ts, _, _ = HDR.unpack_from(data)
        self.up.on_packet(seq, ts / 1e9 - self.extra / 1000, now / 1e9)
        self.last_client = (ts, now, addr)

    def _srv_tx(self) -> None:
        if self.last_client is None:
            return
        n = self._sz(self.size_down, 40, 1400)
        if self.rng.random() < 0.01:
            n = min(1400, n * 4)
        ts, rx, addr = self.last_client
        now = wall_ns()
        pkt = HDR.pack(MAGIC, K_GAME, 0, self.seq_down, 0, now, ts, now - rx) + PAD[: max(0, n - HDR.size)]
        self.seq_down += 1
        self.st.sendto(pkt, addr)

    def _cli_rx(self, data: bytes, addr, now: int) -> None:
        self.down_bytes += len(data) + 28
        _, _, _, seq, _, ts, echo, hold = HDR.unpack_from(data)
        self.down.on_packet(seq, ts / 1e9 - self.extra / 1000, now / 1e9)
        if echo:
            rtt = (now - echo - hold) / 1e6 + 2 * self.extra
            self.rtt_int.append(rtt)
            self.rtt_all.append(rtt)

    def snapshot(self, dt: float) -> dict:
        out = super().snapshot(dt)
        if not hasattr(self, "up"):
            return out
        up, down = self.up.interval(), self.down.interval()
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
        r = self.rtt_all
        jitter = sum(abs(r[i + 1] - r[i]) for i in range(len(r) - 1)) / (len(r) - 1) if len(r) > 2 else 0.0
        loss = (self.up.total_loss_pct() + self.down.total_loss_pct()) / 2
        s.update(rtt_ms=rtt, loss_pct=loss, jitter_ms=jitter, base_rtt_ms=self.base_rtt_ms,
                 added_latency_p95_ms=(rtt["p95"] - self.base_rtt_ms) if rtt["n"] else None,
                 added_latency_p99_ms=(rtt["p99"] - self.base_rtt_ms) if rtt["n"] else None,
                 mos=M.gaming_mos(rtt["p95"], jitter, loss) if rtt["n"] else None)
        return s


# ============================================================================ video call
class _Side:
    """One endpoint of a call: sends audio+video, receives the other direction."""

    def __init__(self, max_kbps: float, fps: float) -> None:
        self.rate = 1200.0
        self.max_kbps = max_kbps
        self.min_kbps = 300.0
        self.fps = fps
        self.seq_a = self.seq_v = self.frame = 0
        self.rates: list[float] = []
        # receiver state for the media arriving at this side
        self.audio = OwdTracker()
        self.frames: dict[int, list] = {}
        self.frames_total = self.frames_late = 0
        self.int_frames = [0, 0]
        self.rep = [0.0, 0, 0, 0]  # owd_sum, n, exp0, recv0
        self.min_owd = 1e9
        self.last_q = 0.0
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.addr = None

    def on_report(self, loss: float, q: float, trend: float) -> None:
        r = self.rate
        if loss > 0.10:
            r *= 1 - 0.5 * loss
        elif q > 60 or (q > 25 and trend > 2):
            r *= 0.90
        elif loss < 0.02 and q < 15:
            r = r * 1.04 + 8
        self.rate = min(self.max_kbps, max(self.min_kbps, r))


class LiveVideoCall(LiveApp):
    app_type = App.VIDEO_CALL
    FREEZE_MS = 250.0

    async def start(self) -> None:
        await super().start()
        p = self.params
        port = int(p.get("port", 3478))
        fps = float(p.get("fps", 30))
        srv = self.env.srv_ip(self.server)
        self.psock = ns_socket(self.env.inet_ns, bind=(srv, port))
        self.csock = ns_socket(self.env.dev_ns(self.device), bind=("0.0.0.0", 0))
        self.csock.connect((srv, port))
        self.socks += [self.psock, self.csock]
        self.register("udp", self.csock)
        # client sends uplink media, peer sends downlink media
        self.mos_hist: list[float] = []
        self.cli = _Side(float(p.get("max_up_kbps", 3000)), fps)
        self.peer = _Side(float(p.get("max_down_kbps", 2500)), fps)
        self.cli.transport = await udp(self.csock, lambda d, a, t: self._rx(self.cli, d, a, t, False))
        self.peer.transport = await udp(self.psock, lambda d, a, t: self._rx(self.peer, d, a, t, True))
        self.endpoints += [self.cli.transport, self.peer.transport]
        for side, up in ((self.cli, True), (self.peer, False)):
            self.spawn(ticker(lambda: 0.020, lambda s=side, u=up: self._audio(s, u), lambda: self.active))
            self.spawn(self._video_loop(side, up))
            self.spawn(ticker(lambda: 0.100, lambda s=side, u=up: self._report(s, u), lambda: self.active))

    def _send(self, side: _Side, up: bool, kind: int, seq: int, frame: int, size: int) -> None:
        if side.transport is None or (not up and side.addr is None):
            return
        pkt = HDR.pack(MAGIC, kind, 0, seq, frame, wall_ns(), 0, 0) + PAD[: max(0, size - 28 - HDR.size)]
        if up:
            self.up_bytes += size
            side.transport.sendto(pkt)
        else:
            side.transport.sendto(pkt, side.addr)

    def _audio(self, side: _Side, up: bool) -> None:
        self._send(side, up, K_AUDIO, side.seq_a, 0, 200)
        side.seq_a += 1

    async def _video_loop(self, side: _Side, up: bool) -> None:
        nxt = mono()
        while self.active:
            key = side.frame % 60 == 0
            avg = side.rate * 1000 / 8 / side.fps
            size = avg * (3.0 if key else 0.96) * math.exp(self.rng.gauss(0, 0.15))
            n = max(1, int(math.ceil(size / 1150)))
            per = int(size / n) + 40
            gap = per * 8 / (2.5 * side.rate * 1000)
            for i in range(n):
                self._send(side, up, K_VIDEO, side.seq_v + i, (side.frame << 8) | min(255, n), per)
                if gap > 0.0008 and i % 2 == 1:
                    await asyncio.sleep(gap * 2)
            side.seq_v += n
            side.frame += 1
            side.rates.append(side.rate)
            nxt += 1.0 / side.fps
            d = nxt - mono()
            await asyncio.sleep(d if d > 0 else 0)
            if d <= 0:
                nxt = mono()

    def _rx(self, rx: _Side, data: bytes, addr, now: int, at_peer: bool) -> None:
        if at_peer:
            rx.addr = addr  # learn the client's address for downlink media
        else:
            self.down_bytes += len(data) + 28
        _, kind, _, seq, frame, ts, echo, hold = HDR.unpack_from(data)
        t_send = ts / 1e9 - self.extra / 1000
        t_now = now / 1e9
        if kind == K_AUDIO:
            owd = rx.audio.on_packet(seq, t_send, t_now)
            rx.rep[0] += owd
            rx.rep[1] += 1
            rx.min_owd = min(rx.min_owd, owd)
        elif kind == K_VIDEO:
            fno, n = frame >> 8, frame & 0xFF
            st = rx.frames.get(fno)
            if st is None:
                st = rx.frames[fno] = [n, 0, t_send, t_now]
            st[1] += 1
            st[3] = t_now
        elif kind == K_REPORT:
            # report about the media *this* side sent: loss/qdelay/trend packed in seq/frame/echo
            rx.on_report(seq / 10000.0, frame / 100.0, echo / 100.0)

    def _report(self, side: _Side, up: bool) -> None:
        """`side` reports on the media it received; the report travels to the other side."""
        exp = side.audio.max_seq + 1
        d_exp = exp - side.rep[2]
        d_recv = side.audio.recv - side.rep[3]
        loss = max(0.0, (d_exp - d_recv) / d_exp) if d_exp > 0 else 0.0
        q = (side.rep[0] / side.rep[1] - side.min_owd) if side.rep[1] else side.last_q
        trend = q - side.last_q
        side.last_q = q
        side.rep = [0.0, 0, exp, side.audio.recv]
        self._close_frames(side)
        pkt = HDR.pack(MAGIC, K_REPORT, 0, int(loss * 10000), int(max(0.0, q) * 100), wall_ns(),
                       int(trend * 100), 0)
        if up and side.transport is not None:
            side.transport.sendto(pkt)
        elif not up and side.transport is not None and side.addr is not None:
            side.transport.sendto(pkt, side.addr)

    def _close_frames(self, side: _Side) -> None:
        now = wall()
        for f in [f for f, st in side.frames.items() if now - st[2] > 0.5]:
            n, got, t_cap, t_last = side.frames.pop(f)
            side.frames_total += 1
            side.int_frames[0] += 1
            if got < n or (t_last - t_cap) * 1000 > self.FREEZE_MS:
                side.frames_late += 1
                side.int_frames[1] += 1

    def _dir(self, side: _Side, sender: _Side) -> dict:
        samples = list(side.audio.int_samples)
        a = side.audio.interval()
        tot, frz = side.int_frames
        side.int_frames = [0, 0]
        mos = M.voice_mos(samples, a["loss_pct"], a["jitter_ms"])["mos"]
        return {"owd": a, "freeze_pct": (frz / tot * 100) if tot else 0.0, "rate_kbps": sender.rate, "mos": mos}

    def snapshot(self, dt: float) -> dict:
        out = super().snapshot(dt)
        if not hasattr(self, "cli"):
            return out
        dn = self._dir(self.cli, self.peer)  # media received by the client = downlink
        up = self._dir(self.peer, self.cli)
        mos_vals = [m for m in (dn["mos"], up["mos"]) if m is not None]
        freeze = max(dn["freeze_pct"], up["freeze_pct"])
        mos = max(1.0, min(mos_vals) - 0.02 * freeze) if mos_vals else None
        p50 = [x for x in (dn["owd"]["p50"], up["owd"]["p50"]) if x is not None]
        p95 = [x for x in (dn["owd"]["p95"], up["owd"]["p95"]) if x is not None]
        p99 = [x for x in (dn["owd"]["p99"], up["owd"]["p99"]) if x is not None]
        out.update(owd_down_ms=dn["owd"], owd_up_ms=up["owd"], latency_ms=max(p50) if p50 else None,
                   latency_p95_ms=max(p95) if p95 else None, latency_p99_ms=max(p99) if p99 else None,
                   jitter_ms=max(dn["owd"]["jitter_ms"], up["owd"]["jitter_ms"]),
                   loss_pct=max(dn["owd"]["loss_pct"], up["owd"]["loss_pct"]), freeze_pct=freeze,
                   video_kbps_down=self.peer.rate, video_kbps_up=self.cli.rate,
                   base_owd_ms=self.base_owd, mos=mos)
        if mos is not None and self.active:
            self.mos_hist.append(mos)
        return out

    def summary(self) -> dict:
        s = super().summary()
        res = {}
        mos_all = []
        for side, sender, name in ((self.cli, self.peer, "down"), (self.peer, self.cli, "up")):
            self._close_frames(side)
            owd = M.summarize_latency(side.audio.all_samples)
            loss = side.audio.total_loss_pct()
            jit = side.audio.jit.j * 1000
            v = M.voice_mos(side.audio.all_samples, loss, jit)
            freeze = side.frames_late / side.frames_total * 100 if side.frames_total else 0.0
            rates = sender.rates
            res[name] = {"owd_ms": owd, "loss_pct": loss, "jitter_ms": jit, "voice_mos": v["mos"],
                         "freeze_pct": freeze, "avg_video_kbps": sum(rates) / len(rates) if rates else 0.0}
            if v["mos"] is not None:
                mos_all.append(max(1.0, v["mos"] - 0.02 * freeze))
        p95 = [res[d]["owd_ms"]["p95"] for d in res if res[d]["owd_ms"]["p95"] is not None]
        p99 = [res[d]["owd_ms"]["p99"] for d in res if res[d]["owd_ms"]["p99"] is not None]
        # call MOS = mean of the per-second E-model MOS: an adaptive jitter buffer re-centres on the
        # current delay within seconds, so quality is judged second by second, not against one
        # run-wide median (which would count slow delay drift as late loss)
        hist = getattr(self, "mos_hist", [])
        run_mos = sum(hist) / len(hist) if hist else (min(mos_all) if mos_all else None)
        s.update(directions=res, mos=run_mos, mos_whole_run=min(mos_all) if mos_all else None,
                 loss_pct=max(res["down"]["loss_pct"], res["up"]["loss_pct"]),
                 owd_p95_ms=max(p95) if p95 else None, owd_p99_ms=max(p99) if p99 else None,
                 added_latency_p95_ms=(max(p95) - self.base_owd) if p95 else None,
                 added_latency_p99_ms=(max(p99) - self.base_owd) if p99 else None,
                 freeze_pct=max(res["down"]["freeze_pct"], res["up"]["freeze_pct"]),
                 video_kbps_down=res["down"]["avg_video_kbps"], video_kbps_up=res["up"]["avg_video_kbps"],
                 jitter_ms=max(res["down"]["jitter_ms"], res["up"]["jitter_ms"]))
        return s


# ============================================================================ TCP helpers
async def tcp_connect(env: Env, device: str, ip: str, port: int) -> socket.socket:
    s = ns_socket(env.dev_ns(device), socket.AF_INET, socket.SOCK_STREAM, reuse=False)
    set_cc(s)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    await asyncio.get_running_loop().sock_connect(s, (ip, port))
    return s


async def recv_exact(sock: socket.socket, n: int, buf: bytearray, on_bytes: Callable[[int], None]) -> None:
    loop = asyncio.get_running_loop()
    view = memoryview(buf)
    got = 0
    while got < n:
        k = await loop.sock_recv_into(sock, view[: min(len(buf), n - got)])
        if k == 0:
            raise ConnectionError("closed")
        got += k
        on_bytes(k)


# ============================================================================ streaming (ABR)
class LiveStreaming(LiveApp):
    app_type = App.STREAMING

    async def start(self) -> None:
        await super().start()
        p = self.params
        top = int(p.get("top_kbps", 16000))
        self.ladder = [b for b in NETFLIX_LADDER_KBPS if b <= top]
        self.seg_s = float(p.get("segment_s", 4.0))
        self.max_buf = float(p.get("max_buffer_s", 30.0))
        self.buffer = 0.0
        self.t_buf = mono()
        self.playing = False
        self.started_play: Optional[float] = None
        self.stall_start: Optional[float] = None
        self.stalls: list[list[float]] = []  # [start, end|None]
        self.level = 0
        self.tputs: list[float] = []
        self.seg_bitrates: list[float] = []
        self.switches = 0
        self.spawn(self._run())

    def _advance(self) -> None:
        now = mono()
        if self.playing and self.stall_start is None:
            left = self.buffer - (now - self.t_buf)
            if left <= 0:
                self.stall_start = self.t_buf + self.buffer
                self.stalls.append([self.stall_start, None])
                self.buffer = 0.0
            else:
                self.buffer = left
        self.t_buf = now

    def _choose(self) -> int:
        if not self.tputs:
            return 0
        recent = self.tputs[-3:]
        est = len(recent) / sum(1.0 / max(t, 1.0) for t in recent)
        b = self.buffer
        target = est * (0.9 if b > 15 else (0.75 if b > 6 else 0.5))
        lvl = 0
        for i, br in enumerate(self.ladder):
            if br <= target:
                lvl = i
        lvl = min(lvl, self.level + (4 if b >= 8 else 2))
        return min(lvl, len(self.ladder) - 1)

    async def _run(self) -> None:
        sock = await tcp_connect(self.env, self.device, self.env.srv_ip(self.server), 443)
        self.socks.append(sock)
        self.register("tcp", sock)
        loop = asyncio.get_running_loop()
        buf = bytearray(256 * 1024)

        def on_bytes(k: int) -> None:
            self.down_bytes += int(k * WIRE)

        while self.active:
            self._advance()
            if self.buffer + self.seg_s > self.max_buf:
                await asyncio.sleep(self.buffer + self.seg_s - self.max_buf)
                continue
            new = self._choose()
            if self.seg_bitrates and new != self.level:
                self.switches += 1
            self.level = new
            nbytes = int(self.ladder[self.level] * 1000 / 8 * self.seg_s * self.rng.uniform(0.9, 1.1))
            t0 = mono()
            await loop.sock_sendall(sock, f"GET {nbytes}\n".encode().ljust(420, b" "))
            self.up_bytes += 420
            await recv_exact(sock, nbytes, buf, on_bytes)
            dt = max(1e-3, mono() - t0)
            self.tputs.append(nbytes * 8 / 1000 / dt)
            self.seg_bitrates.append(self.ladder[self.level] / 1000)
            self._advance()
            self.buffer += self.seg_s
            if self.stall_start is not None and self.buffer >= self.seg_s:
                self.stalls[-1][1] = mono()
                self.stall_start = None
            if not self.playing and self.buffer >= self.seg_s:
                self.playing = True
                self.started_play = mono()

    def _stall_time(self, a: float, b: float) -> float:
        tot = 0.0
        for s, e in self.stalls:
            e = e if e is not None else mono()
            lo, hi = max(a, s), min(b, e)
            if hi > lo:
                tot += hi - lo
        return tot

    def snapshot(self, dt: float) -> dict:
        out = super().snapshot(dt)
        if not hasattr(self, "ladder"):
            return out
        self._advance()
        now = mono()
        stall = self._stall_time(now - dt, now)
        br = self.ladder[self.level] / 1000 if self.seg_bitrates else None
        out.update(bitrate_mbps=br, buffer_s=self.buffer, stalled=self.stall_start is not None, rebuffer_s=stall,
                   startup_s=(self.started_play - self.t_start) if self.started_play else None,
                   mos=M.streaming_mos(br, stall / dt if dt else 0.0, 0.0) if br is not None else None)
        return out

    def summary(self) -> dict:
        s = super().summary()
        end = self.t_stop or mono()
        play_from = self.started_play or end
        stall_time = self._stall_time(play_from, end)
        play_span = max(1e-6, end - play_from)
        rr = stall_time / (play_span) if self.started_play else 1.0
        mins = max(1e-6, end - self.t_start) / 60
        avg = sum(self.seg_bitrates) / len(self.seg_bitrates) if self.seg_bitrates else None
        s.update(avg_bitrate_mbps=avg, rebuffer_ratio=rr, rebuffer_s=stall_time, stalls=len(self.stalls),
                 startup_s=(self.started_play - self.t_start) if self.started_play else None,
                 switches=self.switches, segments=len(self.seg_bitrates),
                 qoe_lin=M.qoe_lin(self.seg_bitrates, stall_time),
                 mos=M.streaming_mos(avg, rr, len(self.stalls) / mins, self.switches / mins) if avg else 1.0)
        return s


# ============================================================================ web
class LiveWeb(LiveApp):
    app_type = App.WEB

    async def start(self) -> None:
        await super().start()
        self.think = float(self.params.get("think_s", 6.0))
        self.plt_int: list[float] = []
        self.plt_all: list[float] = []
        self.spawn(self._run())

    async def _run(self) -> None:
        await asyncio.sleep(self.rng.uniform(0.2, 2.0))
        while self.active:
            try:
                await asyncio.wait_for(self._page(), timeout=30)
            except (asyncio.TimeoutError, OSError, ConnectionError):
                self.plt_int.append(30.0)
                self.plt_all.append(30.0)
            await asyncio.sleep(max(1.0, self.rng.expovariate(1.0 / self.think)))

    async def _page(self) -> None:
        rng = self.rng
        objs = [int(min(2e6, rng.lognormvariate(math.log(18e3), 1.1))) for _ in range(rng.randint(8, 30))]
        main = int(min(1e6, rng.lognormvariate(math.log(60e3), 0.6)))
        nconn = min(6, 1 + len(objs) // 4)
        t0 = mono()
        main_done = asyncio.Event()
        loop = asyncio.get_running_loop()
        socks: list[socket.socket] = []

        def on_bytes(k: int) -> None:
            self.down_bytes += int(k * WIRE)

        async def worker(first: bool) -> None:
            s = await tcp_connect(self.env, self.device, self.env.srv_ip(self.server), 443)
            socks.append(s)
            self.register("tcp", s)
            buf = bytearray(64 * 1024)
            await loop.sock_sendall(s, b"HELLO".ljust(319, b" ") + b"\n")
            await recv_exact(s, 4200, buf, on_bytes)
            if first:
                await loop.sock_sendall(s, f"GET {main}\n".encode().ljust(420, b" "))
                await recv_exact(s, main, buf, on_bytes)
                main_done.set()
            else:
                await main_done.wait()
            while objs:
                n = objs.pop()
                await loop.sock_sendall(s, f"GET {n}\n".encode().ljust(420, b" "))
                self.up_bytes += 420
                await recv_exact(s, n, buf, on_bytes)

        try:
            await asyncio.gather(*(worker(i == 0) for i in range(nconn)))
            plt = mono() - t0 + 4 * self.extra / 1000 * 3  # 3 round trips of base path delay per page
            self.plt_int.append(plt)
            self.plt_all.append(plt)
        finally:
            for s in socks:
                s.close()

    def snapshot(self, dt: float) -> dict:
        out = super().snapshot(dt)
        if not hasattr(self, "plt_int"):
            return out
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


# ============================================================================ bulk / upload
class LiveBulk(LiveApp):
    app_type = App.BULK

    async def start(self) -> None:
        await super().start()
        self.upload = bool(self.params.get("upload", False))
        if self.upload:
            self.app_type = App.UPLOAD
        self.n = int(self.params.get("n_flows", 4))
        self.flows_open = 0
        for i in range(self.n):
            self.spawn(self._one(i * 0.15 + self.rng.random() * 0.05))

    async def _one(self, delay: float) -> None:
        await asyncio.sleep(delay)
        loop = asyncio.get_running_loop()
        s = await tcp_connect(self.env, self.device, self.env.srv_ip(self.server), 443)
        self.socks.append(s)
        self.register("tcp", s)
        self.flows_open += 1
        if self.upload:
            await loop.sock_sendall(s, b"PUT".ljust(63, b" ") + b"\n")
            view = memoryview(BUF)
            while self.active:
                await loop.sock_sendall(s, view)
                self.up_bytes += int(len(view) * WIRE)
        else:
            await loop.sock_sendall(s, b"GET inf".ljust(63, b" ") + b"\n")
            buf = bytearray(256 * 1024)
            view = memoryview(buf)
            while self.active:
                k = await loop.sock_recv_into(s, view)
                if k == 0:
                    break
                self.down_bytes += int(k * WIRE)

    def snapshot(self, dt: float) -> dict:
        out = super().snapshot(dt)
        mbps = (out["kbps_up"] if getattr(self, "upload", False) else out["kbps_down"]) / 1000
        out.update(throughput_mbps=mbps, flows=getattr(self, "flows_open", 0))
        return out

    def summary(self) -> dict:
        s = super().summary()
        end = self.t_stop or mono()
        span = max(1e-6, end - self.t_start)
        tot = self.up_bytes if self.upload else self.down_bytes
        s.update(avg_throughput_mbps=tot * 8 / 1e6 / span, flows=self.n, upload=self.upload)
        return s


LIVE_APPS = {
    App.GAMING: LiveGaming,
    App.VIDEO_CALL: LiveVideoCall,
    App.STREAMING: LiveStreaming,
    App.WEB: LiveWeb,
    App.BULK: LiveBulk,
    App.UPLOAD: LiveBulk,
}


# ============================================================================ content server
class ContentServer:
    """One HTTP-ish server per Internet IP: HELLO (TLS-like), GET <n>, GET inf, PUT."""

    def __init__(self, ns: str, ip: str, port: int = 443) -> None:
        self.ns, self.ip, self.port = ns, ip, port
        self.tasks: set[asyncio.Task] = set()

    async def serve(self) -> None:
        loop = asyncio.get_running_loop()
        ls = ns_socket(self.ns, socket.AF_INET, socket.SOCK_STREAM, bind=(self.ip, self.port))
        set_cc(ls)
        ls.listen(512)
        self.ls = ls
        while True:
            c, _ = await loop.sock_accept(ls)
            c.setblocking(False)
            set_cc(c)
            t = loop.create_task(self._handle(c))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

    async def _handle(self, c: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        pending = b""
        view = memoryview(BUF)
        rbuf = bytearray(256 * 1024)
        try:
            while True:
                while b"\n" not in pending:
                    chunk = await loop.sock_recv(c, 4096)
                    if not chunk:
                        return
                    pending += chunk
                line, pending = pending.split(b"\n", 1)
                parts = line.split()
                if not parts:
                    continue
                cmd = parts[0]
                if cmd == b"HELLO":
                    await loop.sock_sendall(c, view[:4200])
                elif cmd == b"GET" and len(parts) > 1 and parts[1] == b"inf":
                    while True:
                        await loop.sock_sendall(c, view)
                elif cmd == b"GET":
                    n = int(parts[1])
                    while n > 0:
                        k = min(n, len(view))
                        await loop.sock_sendall(c, view[:k])
                        n -= k
                elif cmd == b"PUT":
                    rv = memoryview(rbuf)
                    while True:
                        if await loop.sock_recv_into(c, rv) == 0:
                            return
        except (OSError, ConnectionError, ValueError):
            return
        finally:
            c.close()
