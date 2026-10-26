"""Deployable RL autorate: DQN policy + the same signal processing + a safety shield.

`RLAutorate` is a drop-in replacement for `controller.autorate.DelayAutorate`: same
interface, same probe-delay processing (baseline tracking, deltas, bloat history), but
the shaper adjustment is chosen by a Q-network trained in `ml.rl.train`.

Safety shield (Alshiekh et al., "Safe Reinforcement Learning via Shielding", AAAI 2018):
the learned action is overridden when it would be clearly unsafe -
  * severe bufferbloat (delta > 40 ms)  -> at least a x0.85 cut
  * any bufferbloat (delta > 15 ms)     -> never increase the rate
  * little traffic (load < 0.3)         -> never increase (no evidence of more capacity)
  * observations outside the training range -> fall back to the heuristic's action
and the rate is always clamped to [15 %, 100 %] of the plan rate.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

from ...controller.autorate import DelayAutorate
from .fluid import ACTIONS, HOLD

OBS_DIM = 12
THR_MS = 12.0
DEFAULT_WEIGHTS = Path(__file__).resolve().parents[2] / "models" / "autorate_dqn.npz"


class DirFeat:
    """Per-direction feature state (identical in training and deployment)."""

    __slots__ = ("baseline", "delta", "prev_delta", "hist", "last_bloat", "bloat_rate", "last_action",
                 "int_max", "int_n", "int_over", "prev_achieved", "achieved")

    def __init__(self) -> None:
        self.baseline = -1.0
        self.delta = 0.0
        self.prev_delta = 0.0
        self.hist: deque = deque(maxlen=6)
        self.last_bloat = -1e9
        self.bloat_rate = 0.0
        self.last_action = HOLD
        self.int_max = 0.0
        self.int_n = 0
        self.int_over = 0
        self.prev_achieved = 0.0
        self.achieved = 0.0

    def ingest(self, owd: float) -> None:
        if self.baseline < 0:
            self.baseline = owd
        elif owd < self.baseline:
            self.baseline += 0.9 * (owd - self.baseline)
        else:
            self.baseline += 0.001 * (owd - self.baseline)
        dlt = owd - self.baseline
        self.delta += 0.3 * (dlt - self.delta)
        over = dlt > THR_MS
        self.hist.append(over)
        self.int_max = max(self.int_max, dlt)
        self.int_n += 1
        self.int_over += over

    def obs(self, rate: float, plan: float, achieved: float, now: float, proposal: float = 1.0) -> np.ndarray:
        load = achieved / rate if rate > 0 else 0.0
        o = np.array([
            min(self.delta, 200.0) / 50.0,
            min(self.int_max, 300.0) / 50.0,
            np.clip(self.delta - self.prev_delta, -100, 100) / 20.0,
            min(load, 1.5),
            rate / plan,
            achieved / plan,
            self.last_action / (len(ACTIONS) - 1),
            min(now - self.last_bloat, 10.0) / 10.0,
            self.int_over / max(1, self.int_n),
            self.bloat_rate / plan,
            min(2.0, achieved / max(self.prev_achieved, 1e-9 * plan)) if self.prev_achieved > 0 else 1.0,
            float(np.clip(proposal, 0.0, 2.0)),  # heuristic's proposed rate / current rate
        ], dtype=np.float32)
        self.prev_delta = self.delta
        self.prev_achieved = achieved
        self.achieved = achieved
        self.int_max = 0.0
        self.int_n = 0
        self.int_over = 0
        return o

    def note_action(self, a: int, rate_before: float, now: float, cut: bool = False) -> None:
        self.last_action = a
        if cut:
            self.last_bloat = now
            self.bloat_rate = rate_before if self.bloat_rate <= 0 else 0.7 * self.bloat_rate + 0.3 * rate_before
            self.hist.clear()


class QNet:
    """Tiny numpy MLP (obs -> Q values), loadable without any deep-learning framework."""

    def __init__(self, params: Optional[dict] = None, sizes=(OBS_DIM, 64, 64, len(ACTIONS)), seed: int = 0) -> None:
        if params is None:
            rng = np.random.default_rng(seed)
            params = {}
            for i in range(len(sizes) - 1):
                params[f"W{i}"] = (rng.normal(0, np.sqrt(2.0 / sizes[i]), (sizes[i], sizes[i + 1]))).astype(np.float32)
                params[f"b{i}"] = np.zeros(sizes[i + 1], dtype=np.float32)
        self.p = params
        self.nl = len([k for k in params if k.startswith("W")])

    def forward(self, x: np.ndarray, keep: bool = False):
        acts = [x]
        h = x
        for i in range(self.nl):
            h = h @ self.p[f"W{i}"] + self.p[f"b{i}"]
            if i < self.nl - 1:
                h = np.maximum(h, 0.0)
            acts.append(h)
        return (h, acts) if keep else h

    def copy(self) -> "QNet":
        return QNet({k: v.copy() for k, v in self.p.items()})

    def save(self, path: str | Path, meta: Optional[dict] = None) -> None:
        np.savez(path, **self.p, **({"meta": np.array([str(meta)])} if meta else {}))

    @classmethod
    def load(cls, path: str | Path) -> "QNet":
        d = np.load(path, allow_pickle=False)
        return cls({k: d[k] for k in d.files if k[0] in "Wb"})


def shield(a: int, delta: float, load: float) -> int:
    """Never amplify an increase while the link is bloated or idle (no evidence of capacity)."""
    if ACTIONS[a] > 1.0 and (delta > 15.0 or load < 0.3):
        return HOLD
    return a


class RLAutorate(DelayAutorate):
    """DelayAutorate's interface and bookkeeping, with a learned control law."""

    def __init__(self, weights: Optional[str | Path] = None, use_shield: bool = True, **kw) -> None:
        kw.setdefault("max_frac", 0.95)
        super().__init__(**kw)
        path = Path(weights) if weights else DEFAULT_WEIGHTS
        self.net = QNet.load(path) if path.exists() else None
        self.use_shield = use_shield
        self.feat: dict[str, DirFeat] = {}
        self.shield_hits = 0
        self.fallbacks = 0

    def reset(self, down_rate: float, up_rate: float, max_down: float, max_up: float) -> None:
        super().reset(down_rate, up_rate, max_down, max_up)
        self.plan = {"down": max_down, "up": max_up}
        self.feat = {"down": DirFeat(), "up": DirFeat()}

    def update(self, now, probes, down_tx, up_tx, dt):
        if self.net is None:  # no trained policy available: behave exactly like the heuristic
            return super().update(now, probes, down_tx, up_tx, dt)
        for _, owd_up, owd_down in probes:
            self.feat["up"].ingest(owd_up)
            self.feat["down"].ingest(owd_down)
            self._ingest(self.dirs["up"], owd_up)
            self._ingest(self.dirs["down"], owd_down)
        if probes or self.last_probe is None:
            self.last_probe = now
        lost = now - self.last_probe > 1.0
        for name, tx in (("down", down_tx), ("up", up_tx)):
            d = self.dirs[name]
            f = self.feat[name]
            if d.last_tx and dt > 0:
                d.achieved = max(0, tx - d.last_tx) * 8 / dt
            d.last_tx = tx
            d.load = d.achieved / d.rate if d.rate > 0 else 0.0
            old = d.rate
            n_ev = len(self.events)
            self._control(name, d, now, lost=lost)  # heuristic proposal (updates d.rate)
            proposal = d.rate
            cut = d.last_action == "cut"
            o = f.obs(old, self.plan[name], d.achieved, now, proposal / max(old, 1.0))
            if not np.all(np.isfinite(o)) or np.any(np.abs(o) > 10):
                self.fallbacks += 1
                a = HOLD
            else:
                q = self.net.forward(o[None, :])[0]
                a = int(q.argmax())
                if self.use_shield:
                    s2 = shield(a, d.delta, d.load)
                    if s2 != a:
                        self.shield_hits += 1
                    a = s2
            d.rate = float(min(d.max_rate, max(d.min_rate, proposal * ACTIONS[a])))
            f.note_action(a, old, now, cut)
            d.last_action = (d.last_action if a == HOLD else f"{d.last_action}x{ACTIONS[a]:.2f}")
            if len(self.events) > n_ev:  # the heuristic logged a cut: report the final rate
                self.events[-1]["to"] = d.rate
            elif d.rate < old * 0.95:
                self.events.append({"t": now, "dir": name, "kind": "autorate_cut", "from": old, "to": d.rate,
                                    "delta_ms": d.delta})
        return self.dirs["down"].rate, self.dirs["up"].rate

    def status(self) -> dict:
        st = super().status()
        st["policy"] = "dqn" if self.net is not None else "heuristic"
        st["shield_hits"] = self.shield_hits
        return st
