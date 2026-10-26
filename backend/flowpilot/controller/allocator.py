"""Bandwidth allocation as Network Utility Maximisation (NUM).

Given the shaped capacity R of one direction and per-class demand estimates, choose an
HTB guarantee (`rate`), cap (`ceil`), borrowing priority and buffer size for every class.

Two kinds of traffic need different utility functions (Shenker, "Fundamental design
issues for the future Internet", 1995; Kelly et al., 1998):

* inelastic, latency-critical (game state, calls): utility is a step at the demand - any
  bandwidth below it is nearly worthless, above it is useless. They are served first, at
  their measured peak demand plus headroom, bounded by a protection cap (a misbehaving or
  misclassified flow cannot take the link: cf. RFC 9956 "traffic protection").
* elastic (web, streaming, bulk): concave utility w_k * log(x_k), which makes the optimum
  the *weighted proportionally fair* allocation; with demand caps this is weighted
  water-filling: x_k = min(d_k, w_k * lambda), sum x_k = R_elastic.

Weights come from the household's intent preset (balanced / gaming / work-from-home /
movie night). Floors guarantee that no class ever starves (bulk keeps >= 5 %).
Guarantees are not limits: every elastic class has ceil = R, so unused bandwidth is
borrowed in priority order (work-conserving), exactly HTB's semantics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..common.classes import CLASS_INFO, TC

INELASTIC = (TC.REALTIME, TC.CONFERENCING)
ELASTIC = (TC.INTERACTIVE, TC.STREAMING, TC.BULK)
MTU_B = 1514

INELASTIC_CAP = {TC.REALTIME: 0.20, TC.CONFERENCING: 0.45}
INELASTIC_TOTAL_CAP = 0.55
FLOOR = {TC.INTERACTIVE: 0.05, TC.STREAMING: 0.02, TC.BULK: 0.05}
STREAM_CAP_BPS = 25e6  # per active stream: 4K top rung (16 Mbps) + headroom for buffer refill


@dataclass
class ClassDemand:
    tc: TC
    nflows: int = 0
    arrival_bps: float = 0.0  # offered load (observed before the router's queue)
    peak_bps: float = 0.0  # peak-hold of the offered load over the last seconds
    served_bps: float = 0.0
    backlog_bytes: int = 0
    saturated: bool = False  # the class used (or queued beyond) its allocation recently
    streams: int = 0
    stream_avg_bps: float = 0.0  # sum of the streams' long-term average rates (~ their bitrates)

    @property
    def active(self) -> bool:
        return self.nflows > 0 or self.arrival_bps > 2_000


@dataclass
class ClassPlan:
    tc: TC
    rate: float
    ceil: float
    prio: int
    limit_bytes: int
    demand: float
    note: str = ""


@dataclass
class Allocation:
    capacity: float
    classes: dict = field(default_factory=dict)
    inelastic_scaled: bool = False

    def summary(self) -> dict:
        return {int(k): round(v.rate / 1e6, 3) for k, v in self.classes.items()}


def weighted_waterfill(capacity: float, items: dict) -> dict:
    """items: k -> (weight, floor, cap). Returns k -> allocation with sum <= capacity."""
    floors = {k: min(f, c) for k, (w, f, c) in items.items()}
    tot_floor = sum(floors.values())
    if tot_floor >= capacity:
        s = capacity / tot_floor if tot_floor > 0 else 0.0
        return {k: f * s for k, f in floors.items()}
    alloc = dict(floors)
    remaining = capacity - tot_floor
    active = {k for k, (w, f, c) in items.items() if c > alloc[k] + 1e-9 and w > 0}
    while remaining > 1e-6 and active:
        wsum = sum(items[k][0] for k in active)
        capped = [k for k in active if alloc[k] + remaining * items[k][0] / wsum >= items[k][2]]
        if not capped:
            for k in active:
                alloc[k] += remaining * items[k][0] / wsum
            remaining = 0.0
            break
        for k in capped:
            remaining -= items[k][2] - alloc[k]
            alloc[k] = items[k][2]
            active.discard(k)
    if remaining > 1e-6:
        alloc[TC.BULK] = alloc.get(TC.BULK, 0.0) + remaining  # left-over: lowest class, still borrowable
    return alloc


def allocate(capacity: float, demands: dict, weights: dict, headroom: float = 1.3) -> Allocation:
    R = capacity
    out = Allocation(capacity=R)
    # ---------------------------------------------------------------- 1. inelastic first
    g = {}
    for k in INELASTIC:
        d = demands.get(k) or ClassDemand(k)
        if d.active:
            need = headroom * max(d.peak_bps, d.arrival_bps) + 64_000 * max(1, d.nflows)
            g[k] = min(INELASTIC_CAP[k] * R, need)
        else:
            g[k] = 0.01 * R
    tot = sum(g.values())
    if tot > INELASTIC_TOTAL_CAP * R:
        s = INELASTIC_TOTAL_CAP * R / tot
        g = {k: v * s for k, v in g.items()}
        out.inelastic_scaled = True
    for k in INELASTIC:
        d = demands.get(k) or ClassDemand(k)
        info = CLASS_INFO[k]
        ceil = min(R, max(2.0 * g[k], g[k] + 2e6)) if d.active else min(R, max(INELASTIC_CAP[k] * R, 1e6))
        limit = max(info.min_buffer_pkts * MTU_B, int(ceil / 8 * info.target_delay_ms / 1000))
        out.classes[k] = ClassPlan(k, g[k], ceil, info.htb_prio, limit, d.peak_bps,
                                   "protected" if d.active else "idle")
    # ---------------------------------------------------------------- 2. elastic: weighted water-filling
    R_el = max(0.0, R - sum(g.values()))
    items = {}
    for k in ELASTIC:
        d = demands.get(k) or ClassDemand(k)
        w = float(weights.get(k, 1.0))
        if not d.active:
            floor = FLOOR[k] * R if k == TC.INTERACTIVE else 8_000.0
            items[k] = (w, floor, floor)
            continue
        if k == TC.STREAMING:
            cap = max(1, d.streams) * STREAM_CAP_BPS
            dem = cap if d.saturated else min(cap, 1.3 * max(d.peak_bps, d.arrival_bps) + 1e6)
        else:
            dem = math.inf if d.saturated else 1.25 * max(d.peak_bps, d.arrival_bps) + 0.5e6
        floor = FLOOR[k] * R
        items[k] = (w, min(floor, dem), max(dem, floor))
    x = weighted_waterfill(R_el, items)
    for k in ELASTIC:
        d = demands.get(k) or ClassDemand(k)
        info = CLASS_INFO[k]
        rate = max(8_000.0, x.get(k, 0.0))
        ceil = R
        if k == TC.STREAMING and d.streams > 0:
            # a buffered player needs its bitrate plus refill headroom, never the whole link; capping
            # its burst at ~2.5x its learned average keeps segment fetches from abruptly starving (and
            # overloading the ISP queue for) everyone else, while leaving room to step up a rung
            learned = max(d.streams * 4e6, 2.5 * d.stream_avg_bps)
            ceil = min(R, max(rate, min(d.streams * STREAM_CAP_BPS, learned)))
        # buffers are sized on the rate at which the queue actually drains: high-priority bursty
        # classes (web) drain at up to `ceil`, the lowest class (bulk) at about its served rate
        drain = ceil if k in (TC.INTERACTIVE, TC.STREAMING) else max(rate, d.served_bps)
        limit = max(info.min_buffer_pkts * MTU_B, int(drain / 8 * info.target_delay_ms / 1000))
        out.classes[k] = ClassPlan(k, rate, ceil, info.htb_prio, limit,
                                   math.inf if d.saturated else d.peak_bps,
                                   "saturated" if d.saturated else ("active" if d.active else "idle"))
    return out
