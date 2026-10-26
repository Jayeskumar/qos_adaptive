"""Capacity tracking ("autorate"): keep the router's shaper just below the true bottleneck.

A home router can only control latency if it is the bottleneck, so the shaper must sit a
little below the access link's real capacity. That capacity is not constant: DOCSIS
segments congest in the evening, DSL retrains, and 5G FWA / LTE capacity swings with radio
conditions. A static shaper is either too low (wasted bandwidth) or, after a capacity
drop, too high - and then the ISP's deep buffer fills again and bufferbloat returns.

`DelayAutorate` follows the approach of cake-autorate (lynxthecat/cake-autorate):
  * 20 Hz probes to a reflector give one-way delays in both directions
  * a slowly-rising minimum tracks each direction's baseline delay
  * "bufferbloat" = at least 3 of the last 6 deltas above a threshold
      -> cut the rate to a fraction of the *achieved* rate (deeper cut for worse bloat)
  * high load and no bloat -> probe upwards multiplicatively (+4 % per 200 ms)
  * low load -> drift back towards the base rate
`ml/rl` contains a learned (DQN) policy with the same interface.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class DirState:
    rate: float
    max_rate: float
    min_rate: float
    base_rate: float
    baseline: float = -1.0
    delta: float = 0.0
    bloat_hist: deque = field(default_factory=lambda: deque(maxlen=6))
    last_tx: int = 0
    achieved: float = 0.0
    load: float = 0.0
    refractory_until: float = 0.0
    last_action: str = "hold"
    n_samples: int = 0
    bloat_rate: float = 0.0  # EWMA of the shaper rates at which bufferbloat was detected
    last_bloat: float = -1e9


class DelayAutorate:
    interval = 0.2

    def __init__(self, thr_ms: float = 12.0, up_factor: float = 1.04, high_load: float = 0.80,
                 min_frac: float = 0.15, base_frac: float = 0.90, max_frac: float = 1.0) -> None:
        self.thr_ms = thr_ms
        self.up_factor = up_factor
        self.high_load = high_load
        self.min_frac = min_frac
        self.base_frac = base_frac
        self.max_frac = max_frac
        self.dirs: dict[str, DirState] = {}
        self.events: list[dict] = []
        self.last_probe = None

    def reset(self, down_rate: float, up_rate: float, max_down: float, max_up: float) -> None:
        self.dirs = {
            "down": DirState(down_rate, max_down * self.max_frac, max_down * self.min_frac, max_down * self.base_frac),
            "up": DirState(up_rate, max_up * self.max_frac, max_up * self.min_frac, max_up * self.base_frac),
        }

    # ------------------------------------------------------------------ signal processing
    def _ingest(self, d: DirState, owd: float) -> None:
        if d.baseline < 0:
            d.baseline = owd
        elif owd < d.baseline:
            d.baseline += 0.9 * (owd - d.baseline)
        else:
            d.baseline += 0.001 * (owd - d.baseline)
        delta = owd - d.baseline
        d.delta += 0.3 * (delta - d.delta)
        d.bloat_hist.append(delta > self.thr_ms)
        d.n_samples += 1

    def update(self, now: float, probes: list[tuple[float, float, float]], down_tx: int, up_tx: int,
               dt: float) -> tuple[float, float]:
        for _, owd_up, owd_down in probes:
            self._ingest(self.dirs["up"], owd_up)
            self._ingest(self.dirs["down"], owd_down)
        if probes or self.last_probe is None:
            self.last_probe = now
        # probes missing for a full second under load = the reflector path is congested
        lost = now - self.last_probe > 1.0
        for name, tx in (("down", down_tx), ("up", up_tx)):
            d = self.dirs[name]
            if d.last_tx and dt > 0:
                d.achieved = max(0, tx - d.last_tx) * 8 / dt
            d.last_tx = tx
            d.load = d.achieved / d.rate if d.rate > 0 else 0.0
            self._control(name, d, now, lost=lost)
        return self.dirs["down"].rate, self.dirs["up"].rate

    def _control(self, name: str, d: DirState, now: float, lost: bool) -> None:
        bloated = sum(d.bloat_hist) >= 3 or (lost and d.load > 0.5)
        old = d.rate
        if bloated and now >= d.refractory_until:
            severity = max(0.0, d.delta - self.thr_ms)
            factor = max(0.75, 0.97 - severity / 150.0)
            ref = d.achieved if d.load > 0.4 else d.rate
            d.bloat_rate = d.rate if d.bloat_rate <= 0 else 0.7 * d.bloat_rate + 0.3 * d.rate
            d.last_bloat = now
            d.rate = max(d.min_rate, min(d.rate, ref) * factor)
            d.refractory_until = now + 0.6
            d.bloat_hist.clear()
            d.last_action = "cut"
        elif d.load >= self.high_load and d.delta < self.thr_ms / 2 and now >= d.refractory_until:
            # approach the last known bloat point cautiously, then accelerate the longer the
            # link stays clean (concave-then-convex probing, as in CUBIC): capacity that came
            # back is re-discovered within seconds, a stable ceiling is not hammered
            quiet = now - d.last_bloat
            near = d.bloat_rate > 0 and d.rate > 0.9 * d.bloat_rate
            if near and quiet < 3.0:
                step = 1.003
            elif near and quiet < 8.0:
                step = 1.012
            else:
                step = self.up_factor
            d.rate = min(d.max_rate, d.rate * step)
            if d.rate > d.bloat_rate:
                d.bloat_rate = max(d.bloat_rate, d.rate)  # no bloat up to here: move the memory up
            d.last_action = "probe_up"
        elif d.load < 0.5:
            d.rate += 0.02 * (d.base_rate - d.rate)
            d.last_action = "decay"
        else:
            d.last_action = "hold"
        if d.last_action == "cut" and old - d.rate > 0.05 * old:
            self.events.append({"t": now, "dir": name, "kind": "autorate_cut", "from": old, "to": d.rate,
                                "delta_ms": d.delta})

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    def status(self) -> dict:
        return {k: {"rate": d.rate, "achieved": d.achieved, "delta_ms": d.delta, "baseline_ms": d.baseline,
                    "load": d.load, "action": d.last_action} for k, d in self.dirs.items()}
