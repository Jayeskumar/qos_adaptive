"""FQ-CoDel (RFC 8290 / RFC 8289) and a CAKE-style per-host-fair variant.

Faithful to the Linux implementation in the parts that matter for latency:
  * flows hashed into `buckets` queues (collisions possible, as in Linux)
  * DRR++ with separate new/old flow lists (sparse-flow optimisation)
  * per-queue CoDel with target 5 ms / interval 100 ms and the RFC control law
  * on overflow, head-drop from the fattest queue
CakeLite adds CAKE's host isolation: each flow's DRR quantum is divided by the number of
active flows of its home host, so every device gets an equal share regardless of how
many connections it opens (cf. Høiland-Jørgensen et al., "Piece of CAKE", 2018).
"""
from __future__ import annotations

from collections import deque
from math import sqrt

from .base import Qdisc


class _FlowQ:
    __slots__ = ("q", "bytes", "deficit", "active", "first_above", "drop_next", "count",
                 "lastcount", "dropping", "host")

    def __init__(self, host: str) -> None:
        self.q: deque = deque()
        self.bytes = 0
        self.deficit = 0
        self.active = False
        self.first_above = 0.0
        self.drop_next = 0.0
        self.count = 0
        self.lastcount = 0
        self.dropping = False
        self.host = host


class FQCoDel(Qdisc):
    name = "fq_codel"

    def __init__(self, buckets: int = 1024, quantum: int = 1514, target: float = 0.005,
                 interval: float = 0.100, limit_pkts: int = 10240, mtu: int = 1514,
                 host_fair: bool = False) -> None:
        super().__init__()
        self.buckets = buckets
        self.quantum = quantum
        self.target = target
        self.interval = interval
        self.limit_pkts = limit_pkts
        self.mtu = mtu
        self.host_fair = host_fair
        self.flows: dict[int, _FlowQ] = {}
        self.new_flows: deque = deque()
        self.old_flows: deque = deque()
        self.host_load: dict[str, int] = {}
        self.codel_drops = 0
        self.overlimit_drops = 0

    # ----------------------------------------------------------------- enqueue
    def enqueue(self, pkt, now: float) -> bool:
        idx = pkt.flow.hash % self.buckets
        fq = self.flows.get(idx)
        if fq is None:
            fq = self.flows[idx] = _FlowQ(pkt.flow.lan_ip)
        fq.q.append(pkt)
        fq.bytes += pkt.size
        self.backlog_bytes += pkt.size
        self.backlog_pkts += 1
        if not fq.active:
            fq.active = True
            fq.host = pkt.flow.lan_ip
            if self.host_fair:
                self.host_load[fq.host] = self.host_load.get(fq.host, 0) + 1
            fq.deficit = self._quantum_for(fq)
            self.new_flows.append(fq)
        if self.backlog_pkts > self.limit_pkts:
            self._drop_from_fattest()
            return pkt in fq.q
        return True

    def _quantum_for(self, fq: _FlowQ) -> int:
        if not self.host_fair:
            return self.quantum
        load = self.host_load.get(fq.host, 1) or 1
        return max(64, self.quantum // load)

    def _drop_from_fattest(self) -> None:
        fat = max(self.flows.values(), key=lambda f: f.bytes)
        target_bytes = fat.bytes // 2
        n = 0
        while fat.q and fat.bytes > target_bytes and n < 64:
            p = fat.q.popleft()
            fat.bytes -= p.size
            self.backlog_bytes -= p.size
            self.backlog_pkts -= 1
            self.overlimit_drops += 1
            self._drop(p)
            n += 1

    # ----------------------------------------------------------------- CoDel
    def _do_dequeue(self, fq: _FlowQ, now: float):
        if not fq.q:
            fq.first_above = 0.0
            return None, False
        pkt = fq.q.popleft()
        fq.bytes -= pkt.size
        self.backlog_bytes -= pkt.size
        self.backlog_pkts -= 1
        sojourn = now - pkt.enq_ts
        if sojourn < self.target or fq.bytes <= self.mtu:
            fq.first_above = 0.0
            return pkt, False
        if fq.first_above == 0.0:
            fq.first_above = now + self.interval
            return pkt, False
        return pkt, now >= fq.first_above

    def _codel_dequeue(self, fq: _FlowQ, now: float):
        pkt, ok_to_drop = self._do_dequeue(fq, now)
        if pkt is None:
            fq.dropping = False
            return None
        if fq.dropping:
            if not ok_to_drop:
                fq.dropping = False
            else:
                while now >= fq.drop_next and fq.dropping:
                    self.codel_drops += 1
                    self._drop(pkt)
                    fq.count += 1
                    pkt, ok_to_drop = self._do_dequeue(fq, now)
                    if pkt is None:
                        fq.dropping = False
                        return None
                    if not ok_to_drop:
                        fq.dropping = False
                    else:
                        fq.drop_next = fq.drop_next + self.interval / sqrt(fq.count)
        elif ok_to_drop:
            self.codel_drops += 1
            self._drop(pkt)
            pkt, ok_to_drop = self._do_dequeue(fq, now)
            fq.dropping = True
            delta = fq.count - fq.lastcount
            if delta > 1 and now - fq.drop_next < 16 * self.interval:
                fq.count = delta
            else:
                fq.count = 1
            fq.lastcount = fq.count
            fq.drop_next = now + self.interval / sqrt(fq.count)
        return pkt

    # ----------------------------------------------------------------- dequeue
    def _deactivate(self, fq: _FlowQ) -> None:
        fq.active = False
        if self.host_fair:
            n = self.host_load.get(fq.host, 1) - 1
            if n <= 0:
                self.host_load.pop(fq.host, None)
            else:
                self.host_load[fq.host] = n

    def dequeue(self, now: float):
        new_flows = self.new_flows
        old_flows = self.old_flows
        while True:
            if new_flows:
                lst = new_flows
            elif old_flows:
                lst = old_flows
            else:
                return None, None
            fq = lst[0]
            if fq.deficit <= 0:
                fq.deficit += self._quantum_for(fq)
                lst.popleft()
                old_flows.append(fq)
                continue
            pkt = self._codel_dequeue(fq, now)
            if pkt is None:
                lst.popleft()
                if lst is new_flows and old_flows:
                    old_flows.append(fq)
                else:
                    self._deactivate(fq)
                continue
            fq.deficit -= pkt.size
            self.sent_bytes += pkt.size
            self.sent_pkts += 1
            return pkt, None

    def drain(self) -> list:
        out = []
        for fq in self.flows.values():
            out.extend(fq.q)
            fq.q.clear()
            fq.bytes = 0
            fq.active = False
        self.flows.clear()
        self.new_flows.clear()
        self.old_flows.clear()
        self.host_load.clear()
        self.backlog_bytes = 0
        self.backlog_pkts = 0
        return out

    def stats(self) -> dict:
        s = super().stats()
        s.update(codel_drops=self.codel_drops, overlimit_drops=self.overlimit_drops,
                 active_flows=len(self.new_flows) + len(self.old_flows))
        return s


class CakeLite(FQCoDel):
    """FQ-CoDel + CAKE-style per-host fairness (the parts of CAKE that drive results here)."""

    name = "cake"

    def __init__(self, **kw) -> None:
        # CAKE's 8-way set-associative hash makes collisions rare; a large table models that
        kw.setdefault("buckets", 65536)
        super().__init__(host_fair=True, **kw)
