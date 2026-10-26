from __future__ import annotations

from typing import Callable, Optional


class Qdisc:
    """Interface shared by every queueing discipline.

    enqueue(pkt, now) -> bool      accept or drop the arriving packet
    dequeue(now) -> (pkt, wake)    next packet to transmit, or (None, t) when a shaped
                                   qdisc has backlog but no tokens until time t
    drain() -> list                remove and return everything (used for hot swaps)
    """

    name = "qdisc"

    def __init__(self) -> None:
        self.drop_hook: Optional[Callable] = None
        self.drops = 0
        self.backlog_bytes = 0
        self.backlog_pkts = 0
        self.sent_bytes = 0
        self.sent_pkts = 0

    def _drop(self, pkt) -> None:
        self.drops += 1
        if self.drop_hook is not None:
            self.drop_hook(pkt)

    def enqueue(self, pkt, now: float) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def dequeue(self, now: float):  # pragma: no cover - interface
        raise NotImplementedError

    def drain(self) -> list:  # pragma: no cover - interface
        raise NotImplementedError

    def stats(self) -> dict:
        return {
            "qdisc": self.name,
            "backlog_bytes": self.backlog_bytes,
            "backlog_pkts": self.backlog_pkts,
            "drops": self.drops,
            "sent_bytes": self.sent_bytes,
            "sent_pkts": self.sent_pkts,
        }
