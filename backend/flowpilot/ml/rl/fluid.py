"""Vectorised fluid model of an access link, for training the autorate agent fast.

Each of N parallel environments simulates, at 10 ms resolution:

  C(t)  true bottleneck capacity (normalised to the plan rate = 1.0), drawn from one of
        three families: fixed line with congestion steps, 5G-FWA-like AR(1) with deep
        fades, LTE-like regime switching
  R(t)  the router's shaper rate - the agent's control
  x(t)  aggregate sending rate of capacity-seeking TCP traffic. It chases the delivered
        rate min(R, C) with ~1 RTT lag and a small overshoot (slow start / probing) and
        backs off multiplicatively (x 0.7, CUBIC) when the ISP buffer overflows
  q(t)  ISP queue: dq/dt = x + u - C (u = inelastic load), bounded by the buffer B
        -> one-way delay q / C, sampled by 20 Hz probes with measurement noise

The agent observes exactly what the router can measure (probe delays and its own egress
rate) through the same feature code used in deployment (`policy.AutorateFeatures`).
"""
from __future__ import annotations

import numpy as np

DT = 0.01
SUB = 20  # 200 ms per decision
PROBE_EVERY = 5  # 20 Hz probes
# Residual policy (Silver et al. 2018, Johannink et al. 2019): the agent multiplies the rate
# proposed by the delay-based heuristic by one of these factors. HOLD (x1.0) reproduces the
# heuristic exactly, so the learned policy starts from - and can only refine - a sound controller.
ACTIONS = np.array([0.80, 0.90, 1.00, 1.05, 1.12])
HOLD = 2


class FluidEnv:
    def __init__(self, n: int = 16, seed: int = 0, episode_s: float = 120.0) -> None:
        self.n = n
        self.rng = np.random.default_rng(seed)
        self.steps_per_ep = int(episode_s / (DT * SUB))
        self.reset()

    # ------------------------------------------------------------------ capacity traces
    def _new_trace(self, i: int) -> None:
        rng = self.rng
        kind = rng.choice(["fixed", "fwa", "lte"], p=[0.35, 0.40, 0.25])
        T = self.steps_per_ep * SUB
        c = np.empty(T)
        if kind == "fixed":
            base = rng.uniform(0.85, 1.0)
            c[:] = base
            t = 0
            while t < T:
                t += int(rng.uniform(10, 40) / DT)
                dur = int(rng.uniform(8, 40) / DT)
                if rng.random() < 0.7:
                    c[t:t + dur] = base * rng.uniform(0.3, 0.8)
                t += dur
        elif kind == "fwa":
            mean = rng.uniform(0.35, 0.8)
            vol = rng.uniform(0.05, 0.2)
            x = np.log(mean)
            fade = 0
            per = int(1.0 / DT)
            for k in range(0, T, per):
                x = 0.85 * x + 0.15 * np.log(mean) + rng.normal(0, vol)
                if fade <= 0 and rng.random() < 0.03:
                    fade = int(rng.uniform(2, 8))
                v = np.exp(x) * (rng.uniform(0.3, 0.6) if fade > 0 else 1.0)
                fade -= 1
                c[k:k + per] = np.clip(v, 0.08, 1.0)
            # smooth the 1 s steps a little (radio rate adaptation is not instantaneous)
            kern = np.ones(20) / 20
            c[:] = np.convolve(c, kern, mode="same")
        else:
            levels = np.sort(rng.uniform(0.15, 1.0, size=4))
            t = 0
            while t < T:
                dur = int(rng.uniform(4, 20) / DT)
                c[t:t + dur] = rng.choice(levels)
                t += dur
        self.trace[i] = np.clip(c, 0.05, 1.0)
        self.kind[i] = kind
        self.B[i] = rng.uniform(0.08, 0.8)  # buffer depth in seconds at plan rate
        self.noise[i] = rng.uniform(0.2, 3.0)
        self.u[i] = rng.uniform(0.0, 0.08)
        # transient bursts above the shaper (new flows' slow start, video segment fetches):
        # what makes ingress shaping need a margin below the true capacity
        self.burst_rate[i] = rng.uniform(0.05, 0.6)  # bursts per second
        self.burst_size[i] = rng.uniform(0.1, 0.4)  # extra load as a fraction of the plan rate
        # elastic demand on/off schedule (20 s blocks)
        blocks = int(np.ceil(T / (20 / DT)))
        on = rng.random(blocks) < 0.85
        self.demand[i] = np.repeat(on, int(20 / DT))[:T]

    def reset(self) -> None:
        n = self.n
        T = self.steps_per_ep * SUB
        self.trace = np.zeros((n, T))
        self.demand = np.zeros((n, T), dtype=bool)
        self.kind = np.empty(n, dtype=object)
        self.B = np.zeros(n)
        self.noise = np.zeros(n)
        self.u = np.zeros(n)
        self.burst_rate = np.zeros(n)
        self.burst_size = np.zeros(n)
        self.burst_left = np.zeros(n)
        for i in range(n):
            self._new_trace(i)
        self.t = np.zeros(n, dtype=int)
        self.R = np.full(n, 0.95)
        self.x = np.full(n, 0.5)
        self.q = np.zeros(n)
        self.tx = np.zeros(n)  # cumulative delivered "bytes" (rate-seconds) at the router

    def reset_one(self, i: int) -> None:
        self._new_trace(i)
        self.t[i] = 0
        self.R[i] = 0.95
        self.x[i] = 0.5
        self.q[i] = 0.0
        self.tx[i] = 0.0

    # ------------------------------------------------------------------ dynamics
    def advance(self) -> tuple[list, np.ndarray, np.ndarray, np.ndarray]:
        """Run one decision interval with the current R. Returns per-env probe samples
        (list of arrays of OWD ms), delivered fraction of capacity, mean delay, loss flag."""
        n = self.n
        probes = [[] for _ in range(n)]
        delivered = np.zeros(n)
        cap_sum = np.zeros(n)
        delay_sum = np.zeros(n)
        loss = np.zeros(n, dtype=bool)
        idx = np.arange(n)
        for k in range(SUB):
            tt = np.minimum(self.t, self.trace.shape[1] - 1)
            C = self.trace[idx, tt]
            dem = self.demand[idx, tt]
            target = np.minimum(self.R, C) * 1.04  # TCP probes slightly above what gets through
            tau = 0.06
            self.x += np.where(dem, (target - self.x) * min(1.0, DT / tau), -self.x * min(1.0, DT / 0.2))
            self.x = np.maximum(self.x, 0.0)
            start = (self.rng.random(n) < self.burst_rate * DT) & (self.burst_left <= 0) & dem
            self.burst_left = np.where(start, self.rng.uniform(0.1, 0.4, n), self.burst_left - DT)
            burst = np.where(self.burst_left > 0, self.burst_size, 0.0)
            # router delivers at most R; what arrives at the ISP is the senders' rate (downlink view)
            arrival = self.x + self.u + burst
            self.q += (arrival - C) * DT
            over = self.q > self.B
            if over.any():
                loss |= over
                self.q = np.minimum(self.q, self.B)
                self.x = np.where(over, self.x * 0.7, self.x)
            self.q = np.maximum(self.q, 0.0)
            out_isp = np.where(self.q > 0, C, np.minimum(arrival, C))
            thru = np.minimum(out_isp, self.R)
            self.tx += thru * DT
            delivered += thru * DT
            cap_sum += C * DT
            d_ms = self.q / C * 1000.0
            delay_sum += d_ms * DT
            if k % PROBE_EVERY == 0:
                noisy = d_ms + 5.0 + np.abs(self.rng.normal(0, self.noise))
                for i in range(n):
                    probes[i].append(noisy[i])
            self.t += 1
        span = SUB * DT
        demand_frac = self.demand[idx, np.minimum(self.t - 1, self.trace.shape[1] - 1)]
        util = np.where(demand_frac, delivered / np.maximum(cap_sum, 1e-9), 1.0)
        return probes, util, delay_sum / span, loss

    def done(self) -> np.ndarray:
        return self.t >= self.steps_per_ep * SUB


def reward(util: np.ndarray, delay_ms: np.ndarray, loss: np.ndarray, changed: np.ndarray) -> np.ndarray:
    """Utilisation minus a delay penalty that grows quickly beyond ~10 ms of ISP queueing."""
    excess = np.maximum(0.0, delay_ms - 5.0)
    r = util - 0.04 * excess - 0.0015 * np.maximum(0.0, delay_ms - 30.0) ** 2 - 0.3 * loss - 0.01 * changed
    return np.maximum(r, -10.0)  # clip catastrophes (multi-second queues) for stable learning
