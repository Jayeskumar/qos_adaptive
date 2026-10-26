"""Hierarchical Token Bucket, modelled on Linux sch_htb.

Semantics reproduced from net/sched/sch_htb.c:
  * every class has a guaranteed `rate` and a `ceil` it may borrow up to
  * class mode: CANT_SEND if ctokens < 0, else CAN_SEND if tokens >= 0, else MAY_BORROW
  * dequeue serves level 0 (leaves sending within their own rate) first, then classes that
    borrow from a level-1 ancestor, and so on; inside a level, lower `prio` goes first and
    equal prios share by DRR with quantum = rate / r2q
  * charging: the lender and every class above it pay tokens, all classes on the path pay
    ctokens; borrowers below the lender only pay ctokens
This is exactly the data-plane FlowPilot programs into the real kernel, so the simulator
and the Linux testbed enforce the same allocation semantics.
"""
from __future__ import annotations

from typing import Callable, Optional

from .base import Qdisc
from .fifo import FIFO

R2Q = 10
MIN_QUANTUM = 1514
MAX_QUANTUM = 200_000


class HTBClass:
    __slots__ = ("cid", "parent", "children", "level", "prio", "rate", "ceil", "burst", "cburst",
                 "tokens", "ctokens", "t_last", "quantum", "deficit", "leaf", "name",
                 "sent_bytes", "sent_pkts", "drops", "lends", "borrows", "_path")

    def __init__(self, cid: int, parent: Optional["HTBClass"], rate: float, ceil: float, prio: int,
                 leaf: Optional[Qdisc], name: str = "") -> None:
        self.cid = cid
        self.parent = parent
        self.children: list[HTBClass] = []
        self.level = 0
        self.prio = prio
        self.name = name or f"1:{cid}"
        self.leaf = leaf
        self.deficit = [0, 0, 0, 0, 0, 0, 0, 0]
        self.sent_bytes = 0
        self.sent_pkts = 0
        self.drops = 0
        self.lends = 0
        self.borrows = 0
        self.t_last = 0.0
        self._path: list[HTBClass] = []
        self.set_rates(rate, ceil)
        self.tokens = self.burst
        self.ctokens = self.cburst

    def set_rates(self, rate: float, ceil: float, burst: Optional[float] = None,
                  cburst: Optional[float] = None) -> None:
        self.rate = max(8_000.0, float(rate))
        self.ceil = max(self.rate, float(ceil))
        # tc's default burst is roughly rate/HZ + mtu; 2 ms of traffic models it well
        self.burst = float(burst) if burst else max(3_000.0, self.rate / 8 * 0.002)
        self.cburst = float(cburst) if cburst else max(3_000.0, self.ceil / 8 * 0.002)
        self.quantum = int(min(MAX_QUANTUM, max(MIN_QUANTUM, self.rate / 8 / R2Q)))

    def update(self, now: float) -> None:
        dt = now - self.t_last
        if dt > 0:
            t = self.tokens + self.rate * dt * 0.125
            self.tokens = t if t < self.burst else self.burst
            c = self.ctokens + self.ceil * dt * 0.125
            self.ctokens = c if c < self.cburst else self.cburst
            self.t_last = now


class HTB(Qdisc):
    name = "htb"

    def __init__(self, root_rate: float, default_cid: int = 30) -> None:
        super().__init__()
        self.root = HTBClass(1, None, root_rate, root_rate, 0, None, "root")
        self.classes: dict[int, HTBClass] = {1: self.root}
        self.default_cid = default_cid
        self.classify: Callable = lambda pkt: default_cid
        self._ptr: dict[tuple[int, int], int] = {}
        self._leaves: list[HTBClass] = []
        self._active: dict[int, HTBClass] = {}  # leaves with backlog (insertion-ordered)

    # ------------------------------------------------------------- configuration
    def add_class(self, cid: int, parent_cid: int, rate: float, ceil: float, prio: int,
                  leaf: Optional[Qdisc] = None, name: str = "") -> HTBClass:
        parent = self.classes[parent_cid]
        if parent.leaf is not None:
            raise ValueError("cannot attach a child to a leaf class")
        if leaf is None:
            leaf = FIFO(limit_pkts=1000)
        leaf.drop_hook = self._leaf_drop
        cl = HTBClass(cid, parent, rate, ceil, prio, leaf, name)
        parent.children.append(cl)
        self.classes[cid] = cl
        self._refresh()
        return cl

    def add_inner(self, cid: int, parent_cid: int, rate: float, ceil: float, prio: int = 0,
                  name: str = "") -> HTBClass:
        parent = self.classes[parent_cid]
        cl = HTBClass(cid, parent, rate, ceil, prio, None, name)
        parent.children.append(cl)
        self.classes[cid] = cl
        self._refresh()
        return cl

    def remove_class(self, cid: int) -> list:
        cl = self.classes.pop(cid)
        cl.parent.children.remove(cl)
        orphans = cl.leaf.drain() if cl.leaf is not None else []
        self._active.pop(cid, None)
        self._refresh()
        return orphans

    def change_class(self, cid: int, rate: Optional[float] = None, ceil: Optional[float] = None,
                     prio: Optional[int] = None, burst: Optional[float] = None) -> None:
        cl = self.classes[cid]
        cl.set_rates(cl.rate if rate is None else rate, cl.ceil if ceil is None else ceil, burst)
        cl.tokens = min(cl.tokens, cl.burst)
        cl.ctokens = min(cl.ctokens, cl.cburst)
        if prio is not None:
            cl.prio = prio

    def set_root_rate(self, rate: float) -> None:
        self.root.set_rates(rate, rate)
        self.root.tokens = min(self.root.tokens, self.root.burst)
        self.root.ctokens = min(self.root.ctokens, self.root.cburst)

    def _refresh(self) -> None:
        def depth(c: HTBClass) -> int:
            return 0 if not c.children else 1 + max(depth(k) for k in c.children)

        for c in self.classes.values():
            c.level = depth(c)
        self._leaves = [c for c in self.classes.values() if c.leaf is not None]
        for c in self._leaves:
            path = []
            x = c
            while x is not None:
                path.append(x)
                x = x.parent
            c._path = path

    def _leaf_drop(self, pkt) -> None:
        self._drop(pkt)

    # ------------------------------------------------------------- data path
    def enqueue(self, pkt, now: float) -> bool:
        cid = self.classify(pkt)
        cl = self.classes.get(cid)
        if cl is None or cl.leaf is None:
            cl = self.classes[self.default_cid]
        ok = cl.leaf.enqueue(pkt, now)
        if cl.leaf.backlog_pkts > 0:
            self._active[cl.cid] = cl
        return ok

    def _lender_level(self, leaf: HTBClass, now: float) -> int:
        for c in leaf._path:
            c.update(now)
            if c.ctokens < 0:
                return -1
            if c.tokens >= 0:
                return c.level
        return -1

    # backlog is derived from the leaves: leaf AQMs may drop packets on their own
    @property
    def backlog_pkts(self) -> int:  # type: ignore[override]
        return sum(c.leaf.backlog_pkts for c in self._leaves)

    @backlog_pkts.setter
    def backlog_pkts(self, _v) -> None:
        pass

    @property
    def backlog_bytes(self) -> int:  # type: ignore[override]
        return sum(c.leaf.backlog_bytes for c in self._leaves)

    @backlog_bytes.setter
    def backlog_bytes(self, _v) -> None:
        pass

    def dequeue(self, now: float):
        cands: dict[tuple[int, int], list[HTBClass]] = {}
        backlogged = False
        active = self._active
        for leaf in list(active.values()):
            if leaf.leaf.backlog_pkts <= 0:
                del active[leaf.cid]
                continue
            backlogged = True
            lvl = self._lender_level(leaf, now)
            if lvl < 0:
                continue
            cands.setdefault((lvl, leaf.prio), []).append(leaf)
        if not backlogged:
            return None, None
        if cands:
            for key in sorted(cands):
                got = self._drr(key, cands[key], now)
                if got is not None:
                    return got, None
        # nothing can send: find when the earliest backlogged leaf becomes eligible
        if self.backlog_pkts <= 0:
            return None, None
        return None, now + self._next_eligible(now)

    def _drr(self, key: tuple[int, int], cands: list[HTBClass], now: float):
        lvl = key[0]
        n = len(cands)
        ptr = self._ptr.get(key, 0)
        for _ in range(3 * n + 2):
            cl = cands[ptr % n]
            if cl.deficit[lvl] <= 0:
                cl.deficit[lvl] += cl.quantum
                ptr += 1
                continue
            pkt, _ = cl.leaf.dequeue(now)
            if pkt is None:
                ptr += 1
                continue
            cl.deficit[lvl] -= pkt.size
            if cl.deficit[lvl] <= 0:
                ptr += 1
            self._ptr[key] = ptr
            self._charge(cl, lvl, pkt.size)
            self.sent_bytes += pkt.size
            self.sent_pkts += 1
            return pkt
        self._ptr[key] = ptr
        return None

    @staticmethod
    def _charge(leaf: HTBClass, lvl: int, size: int) -> None:
        for c in leaf._path:
            if c.level >= lvl:
                c.tokens -= size
                if c.level == lvl and c is not leaf:
                    c.lends += 1
            else:
                c.borrows += 1
            c.ctokens -= size
            c.sent_bytes += size
            c.sent_pkts += 1

    def _next_eligible(self, now: float) -> float:
        best = 0.050
        for leaf in self._active.values():
            if leaf.leaf.backlog_pkts <= 0:
                continue
            ceil_wait = 0.0
            for c in leaf._path:
                if c.ctokens < 0:
                    w = -c.ctokens * 8.0 / c.ceil
                    if w > ceil_wait:
                        ceil_wait = w
                rate_wait = -c.tokens * 8.0 / c.rate if c.tokens < 0 else 0.0
                t = rate_wait if rate_wait > ceil_wait else ceil_wait
                if t < best:
                    best = t
        return max(best, 2e-6)

    def drain(self) -> list:
        out = []
        for leaf in self._leaves:
            out.extend(leaf.leaf.drain())
        self._active.clear()
        return out

    def class_stats(self) -> list[dict]:
        out = []
        for c in self.classes.values():
            d = {
                "cid": c.cid, "name": c.name, "parent": c.parent.cid if c.parent else None,
                "rate": c.rate, "ceil": c.ceil, "prio": c.prio, "sent_bytes": c.sent_bytes,
                "sent_pkts": c.sent_pkts, "leaf": c.leaf is not None, "lends": c.lends, "borrows": c.borrows,
            }
            if c.leaf is not None:
                d.update(backlog_bytes=c.leaf.backlog_bytes, backlog_pkts=c.leaf.backlog_pkts,
                         drops=c.leaf.drops)
            out.append(d)
        return out
