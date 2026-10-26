from __future__ import annotations

from collections import deque
from typing import Optional

from .base import Qdisc


class FIFO(Qdisc):
    """Drop-tail FIFO limited in bytes (bfifo) and/or packets (pfifo)."""

    name = "fifo"

    def __init__(self, limit_bytes: Optional[int] = None, limit_pkts: Optional[int] = 1000) -> None:
        super().__init__()
        self.q: deque = deque()
        self.limit_bytes = limit_bytes
        self.limit_pkts = limit_pkts

    def set_limit(self, limit_bytes: Optional[int] = None, limit_pkts: Optional[int] = None) -> None:
        self.limit_bytes = limit_bytes
        self.limit_pkts = limit_pkts

    def enqueue(self, pkt, now: float) -> bool:
        if (self.limit_pkts is not None and self.backlog_pkts >= self.limit_pkts) or (
            self.limit_bytes is not None and self.backlog_bytes + pkt.size > self.limit_bytes
        ):
            self._drop(pkt)
            return False
        self.q.append(pkt)
        self.backlog_bytes += pkt.size
        self.backlog_pkts += 1
        return True

    def dequeue(self, now: float):
        if self.q:
            pkt = self.q.popleft()
            self.backlog_bytes -= pkt.size
            self.backlog_pkts -= 1
            self.sent_bytes += pkt.size
            self.sent_pkts += 1
            return pkt, None
        return None, None

    def peek_sojourn(self, now: float) -> float:
        return now - self.q[0].enq_ts if self.q else 0.0

    def drain(self) -> list:
        out = list(self.q)
        self.q.clear()
        self.backlog_bytes = 0
        self.backlog_pkts = 0
        return out
