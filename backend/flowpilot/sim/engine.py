"""Minimal, fast discrete-event engine.

Events are (time, seq, fn, args) tuples on a binary heap. Cancellation is done by the
callers with generation counters, which keeps the hot path free of bookkeeping.
"""
from __future__ import annotations

import heapq
import itertools
from typing import Any, Callable


class Sim:
    __slots__ = ("now", "_q", "_seq", "events")

    def __init__(self) -> None:
        self.now = 0.0
        self._q: list[tuple[float, int, Callable[..., Any], tuple]] = []
        self._seq = itertools.count()
        self.events = 0

    def at(self, t: float, fn: Callable[..., Any], *args: Any) -> None:
        heapq.heappush(self._q, (t, next(self._seq), fn, args))

    def after(self, dt: float, fn: Callable[..., Any], *args: Any) -> None:
        heapq.heappush(self._q, (self.now + dt, next(self._seq), fn, args))

    def run_until(self, t_end: float) -> None:
        q = self._q
        pop = heapq.heappop
        n = 0
        while q and q[0][0] <= t_end:
            t, _, fn, args = pop(q)
            self.now = t
            fn(*args)
            n += 1
        self.events += n
        if self.now < t_end:
            self.now = t_end
