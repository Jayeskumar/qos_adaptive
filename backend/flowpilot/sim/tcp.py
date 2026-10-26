"""TCP model: CUBIC + HyStart congestion control, SACK-style recovery, RFC 6298 RTO.

The model works in MSS-sized segments (1448 B payload, 1500 B on the wire). Each
connection carries data in one direction (server->client for downloads, client->server
for uploads); ACKs flow the other way through the *same* bottleneck queues, which is
what makes uplink congestion hurt downloads in real homes.

Accuracy goals (what matters for QoS experiments): capacity-seeking behaviour that fills
drop-tail buffers (bufferbloat), multiplicative decrease on loss, slow-start restart after
idle (affects ON/OFF video streaming), delayed ACKs, and RTO back-off under heavy loss.
"""
from __future__ import annotations

import heapq
import math
from collections import deque
from typing import Callable, Optional

from .net import K_CTRL, K_TCP_ACK, K_TCP_DATA, MTU, Flow, Packet

MSS = 1448
ACK_SIZE = 66
IW = 10
CUBIC_C = 0.4
CUBIC_BETA = 0.7
MIN_RTO = 0.2
DELACK = 0.040


class TcpConn:
    """One TCP connection. `data_up` selects the data direction."""

    def __init__(self, world, flow: Flow, data_up: bool) -> None:
        self.w = world
        self.sim = world.sim
        self.flow = flow
        self.data_up = data_up
        self._send_data = world.send_up if data_up else world.send_down
        self._send_ack = world.send_down if data_up else world.send_up
        if data_up:
            flow.recv_up = self._dispatch_at_server
            flow.recv_down = self._dispatch_at_client
        else:
            flow.recv_down = self._dispatch_at_client
            flow.recv_up = self._dispatch_at_server
        # application framing: (end_seg, callback) in send order
        self.msgs: deque = deque()
        self.on_ctrl_server: Optional[Callable[[Packet], None]] = None
        self.on_ctrl_client: Optional[Callable[[Packet], None]] = None
        self.closed = False
        # ---------------------------------------------------------- sender state
        self.total_segs = 0  # segments made available by the application
        self.next_seg = 0
        self.snd_una = 0
        self.state: dict[int, int] = {}  # 0 in flight, 1 sacked, 2 lost (pending), 3 retransmitted
        self.pipe = 0
        self.lost_q: list[int] = []
        self.scan_ptr = 0
        self.highest_sacked = -1
        self.cwnd = float(IW)
        self.ssthresh = 1e9
        self.srtt: Optional[float] = None
        self.rttvar = 0.0
        self.rto = 1.0
        self.min_rtt = 1e9
        self.rto_deadline = 0.0
        self.rto_pending = False
        self.backoff = 0
        self.in_recovery = False
        self.recover = 0
        self.last_activity = 0.0
        self.w_max = 0.0
        self.k = 0.0
        self.epoch: Optional[float] = None
        self.w_origin = 0.0
        self.retransmits = 0
        self.timeouts = 0
        # ---------------------------------------------------------- receiver state
        self.rcv_nxt = 0
        self.ooo: set[int] = set()
        self.unacked_in_order = 0
        self.delack_gen = 0
        self.delivered_segs = 0
        self.on_delivered: Optional[Callable[[int], None]] = None  # bytes delivered in order

    # ================================================================ app API
    def send_msg(self, nbytes: float, on_complete: Optional[Callable[[], None]] = None) -> None:
        """Queue `nbytes` (may be math.inf) for transmission; callback fires on full delivery."""
        if self.closed:
            return
        now = self.sim.now
        if self.pipe == 0 and self.next_seg >= self.total_segs and now - self.last_activity > self.rto:
            # RFC 5681 restart after idle (Linux tcp_slow_start_after_idle=1)
            self.cwnd = min(self.cwnd, float(IW))
            self.epoch = None
        if math.isinf(nbytes):
            self.total_segs = 1 << 60
        else:
            self.total_segs += max(1, int(math.ceil(nbytes / MSS)))
        if on_complete is not None:
            self.msgs.append((self.total_segs, on_complete))
        self._try_send()

    def close(self) -> None:
        self.closed = True
        self.total_segs = self.next_seg

    def send_ctrl(self, up: bool, size: int, payload: object = None) -> None:
        """Small control message (SYN, HTTP request...). Lost copies are re-sent after an RTO,
        mirroring TCP's own retransmission of those segments."""
        if self.closed:
            return
        pkt = Packet(self.flow, size, K_CTRL, up, self.sim.now, payload=payload)
        pkt.on_drop = self._ctrl_dropped
        (self.w.send_up if up else self.w.send_down)(pkt)

    def _ctrl_dropped(self, pkt: Packet) -> None:
        rto = max(MIN_RTO, self.rto if self.srtt is not None else 1.0)
        self.sim.after(rto, self.send_ctrl, pkt.up, pkt.size, pkt.payload)

    # ================================================================ dispatch
    def _dispatch_at_server(self, pkt: Packet) -> None:
        if pkt.kind == K_CTRL:
            if self.on_ctrl_server is not None:
                self.on_ctrl_server(pkt)
        elif self.data_up:
            self._rx_data(pkt)
        else:
            self._rx_ack(pkt)

    def _dispatch_at_client(self, pkt: Packet) -> None:
        if pkt.kind == K_CTRL:
            if self.on_ctrl_client is not None:
                self.on_ctrl_client(pkt)
        elif self.data_up:
            self._rx_ack(pkt)
        else:
            self._rx_data(pkt)

    # ================================================================ sender
    def _try_send(self) -> None:
        if self.closed and self.next_seg >= self.total_segs and not self.lost_q:
            return
        cwnd = self.cwnd
        while self.pipe < cwnd:
            if self.lost_q:
                seg = heapq.heappop(self.lost_q)
                if self.state.get(seg) != 2:
                    continue
                self._xmit(seg, True)
            elif self.next_seg < self.total_segs:
                seg = self.next_seg
                self.next_seg += 1
                self._xmit(seg, False)
            else:
                break

    def _xmit(self, seg: int, retx: bool) -> None:
        now = self.sim.now
        self.state[seg] = 3 if retx else 0
        self.pipe += 1
        if retx:
            self.retransmits += 1
        self.last_activity = now
        self._send_data(Packet(self.flow, MTU, K_TCP_DATA, self.data_up, now, seq=seg))
        if not self.rto_pending:
            self.rto_deadline = now + self.rto
            self.rto_pending = True
            self.sim.at(self.rto_deadline, self._rto_check)

    def _rto_check(self) -> None:
        now = self.sim.now
        if self.snd_una >= self.next_seg and not self.lost_q:
            self.rto_pending = False
            return
        if now + 1e-9 < self.rto_deadline:
            self.sim.at(self.rto_deadline, self._rto_check)
            return
        # ---- retransmission timeout
        self.timeouts += 1
        self.backoff += 1
        self.rto = min(60.0, self.rto * 2)
        self.w_max = self.cwnd
        self.ssthresh = max(2.0, self.cwnd * CUBIC_BETA)
        self.cwnd = 1.0
        self.epoch = None
        self.in_recovery = False
        self.lost_q = []
        for seg, st in self.state.items():
            if st != 1:
                self.state[seg] = 2
                heapq.heappush(self.lost_q, seg)
        self.pipe = 0
        self.rto_deadline = now + self.rto
        self.sim.at(self.rto_deadline, self._rto_check)
        self._try_send()

    def _rx_ack(self, pkt: Packet) -> None:
        now = self.sim.now
        self.last_activity = now
        rtt = now - pkt.ts
        if rtt > 0:
            self._rtt_sample(rtt)
        state = self.state
        newly = 0
        a = pkt.ack
        if a > self.snd_una:
            for seg in range(self.snd_una, a):
                st = state.pop(seg, None)
                if st == 0 or st == 3:
                    self.pipe -= 1
                    newly += 1
            self.snd_una = a
            if self.scan_ptr < a:
                self.scan_ptr = a
            self.backoff = 0
            self.rto_deadline = now + self.rto
        s = pkt.sack
        if s >= self.snd_una:
            st = state.get(s)
            if st == 0 or st == 3:
                self.pipe -= 1
                state[s] = 1
                newly += 1
            elif st == 2:
                state[s] = 1
            if s > self.highest_sacked:
                self.highest_sacked = s
        # loss detection (RFC 6675 style DupThresh = 3)
        lost = False
        limit = self.highest_sacked - 3
        if limit >= self.scan_ptr:
            seg = self.scan_ptr
            while seg <= limit:
                if state.get(seg) == 0:
                    state[seg] = 2
                    self.pipe -= 1
                    heapq.heappush(self.lost_q, seg)
                    lost = True
                seg += 1
            self.scan_ptr = limit + 1
        if lost and not self.in_recovery:
            self._on_loss(now)
        if self.in_recovery and self.snd_una >= self.recover:
            self.in_recovery = False
        if newly and not self.in_recovery:
            self._grow(newly, now, rtt)
        if self.pipe < 0:
            self.pipe = 0
        self._try_send()

    def _rtt_sample(self, r: float) -> None:
        if r < self.min_rtt:
            self.min_rtt = r
        if self.srtt is None:
            self.srtt = r
            self.rttvar = r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r
        self.rto = max(MIN_RTO, self.srtt + 4 * self.rttvar)

    def _on_loss(self, now: float) -> None:
        self.in_recovery = True
        self.recover = self.next_seg
        self.epoch = None
        if self.cwnd < self.w_max:  # fast convergence
            self.w_max = self.cwnd * (1 + CUBIC_BETA) / 2
        else:
            self.w_max = self.cwnd
        self.ssthresh = max(2.0, self.cwnd * CUBIC_BETA)
        self.cwnd = self.ssthresh

    def _grow(self, acked: int, now: float, rtt: float) -> None:
        if self.cwnd < self.ssthresh:
            self.cwnd += acked
            # HyStart (delay increase): leave slow start once queueing delay builds
            if self.cwnd >= 16 and self.min_rtt < 1e8:
                eta = min(0.016, max(0.004, self.min_rtt / 8))
                if rtt > self.min_rtt + eta:
                    self.ssthresh = self.cwnd
            return
        if self.epoch is None:
            self.epoch = now
            if self.cwnd < self.w_max:
                self.k = ((self.w_max - self.cwnd) / CUBIC_C) ** (1 / 3)
                self.w_origin = self.w_max
            else:
                self.k = 0.0
                self.w_origin = self.cwnd
        srtt = self.srtt or 0.05
        t = now - self.epoch + self.min_rtt
        target = self.w_origin + CUBIC_C * (t - self.k) ** 3
        w_tcp = self.w_max * CUBIC_BETA + 3 * (1 - CUBIC_BETA) / (1 + CUBIC_BETA) * (t / srtt)
        if w_tcp > target:
            target = w_tcp
        if target > self.cwnd:
            inc = (target - self.cwnd) / self.cwnd * acked
            self.cwnd += min(inc, 0.5 * acked)
        else:
            self.cwnd += 0.01 * acked / self.cwnd

    # ================================================================ receiver
    def _rx_data(self, pkt: Packet) -> None:
        seg = pkt.seq
        immediate = False
        sack = -1
        if seg == self.rcv_nxt:
            had_holes = bool(self.ooo)
            self.rcv_nxt += 1
            delivered = 1
            ooo = self.ooo
            while self.rcv_nxt in ooo:
                ooo.discard(self.rcv_nxt)
                self.rcv_nxt += 1
                delivered += 1
            self.delivered_segs += delivered
            if self.on_delivered is not None:
                self.on_delivered(delivered * MSS)
            self._check_msgs()
            self.unacked_in_order += delivered
            immediate = had_holes or self.unacked_in_order >= 2
        elif seg > self.rcv_nxt:
            if seg not in self.ooo:
                self.ooo.add(seg)
            sack = seg
            immediate = True
        else:
            immediate = True  # duplicate: tell the sender quickly
        if immediate:
            self._send_ack_now(pkt.ts, sack)
        else:
            self.delack_gen += 1
            self.sim.after(DELACK, self._delack_fire, self.delack_gen, pkt.ts)

    def _check_msgs(self) -> None:
        # receiver-side framing (the simulator shares the boundaries with the sender)
        while self.msgs and self.msgs[0][0] <= self.rcv_nxt:
            _, cb = self.msgs.popleft()
            cb()

    def _delack_fire(self, gen: int, ts: float) -> None:
        if gen == self.delack_gen and self.unacked_in_order > 0:
            self._send_ack_now(ts, -1)

    def _send_ack_now(self, ts: float, sack: int) -> None:
        self.unacked_in_order = 0
        self.delack_gen += 1
        self._send_ack(Packet(self.flow, ACK_SIZE, K_TCP_ACK, not self.data_up, ts,
                              ack=self.rcv_nxt, sack=sack))
