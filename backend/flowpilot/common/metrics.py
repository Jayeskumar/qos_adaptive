"""Network and Quality-of-Experience metrics.

Every formula here is documented in docs/REPORT.md (section "Metrics"). In short:

* latency percentiles (p50/p95/p99) on one-way delay or RTT samples
* interarrival jitter, RFC 3550 section 6.4.1 (J += (|D| - J) / 16)
* voice MOS from the ITU-T G.107 E-model, in the widely used simplified form of
  Cole & Rosenbluth (ACM CCR 2001): R = 93.2 - Id - Ie,eff
* gaming MOS: interactivity-driven heuristic informed by ITU-T G.1072 and by
  Claypool et al. (latency below 100 ms already measurably hurts FPS players)
* streaming MOS: bitrate utility with stall penalties, the structure used by ITU-T P.1203
  and by the QoE_lin objective of Yin et al. / Pensieve
* web MOS: logarithmic page-load-time mapping as in ITU-T G.1030
* fairness: Jain's index (Jain, Chiu & Hawe, DEC TR-301, 1984) and the QoE fairness
  index F = 1 - 2*sigma/(H - L) (Hossfeld et al., IEEE Comm. Letters 2017)
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np


def pct(samples: Sequence[float], q: float) -> float:
    if not len(samples):
        return float("nan")
    return float(np.percentile(np.asarray(samples, dtype=float), q))


def summarize_latency(samples: Sequence[float]) -> dict:
    if not len(samples):
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    a = np.asarray(samples, dtype=float)
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return {"n": int(a.size), "mean": float(a.mean()), "p50": float(p50), "p95": float(p95),
            "p99": float(p99), "max": float(a.max())}


class Rfc3550Jitter:
    """Running interarrival jitter estimator (RFC 3550 6.4.1), in seconds."""

    __slots__ = ("j", "_last_transit")

    def __init__(self) -> None:
        self.j = 0.0
        self._last_transit: float | None = None

    def update(self, send_ts: float, recv_ts: float) -> float:
        transit = recv_ts - send_ts
        if self._last_transit is not None:
            d = abs(transit - self._last_transit)
            self.j += (d - self.j) / 16.0
        self._last_transit = transit
        return self.j


# ---------------------------------------------------------------- E-model (voice)
def emodel_r(mouth_to_ear_ms: float, loss_pct: float, ie: float = 0.0, bpl: float = 25.1,
             burst_r: float = 1.0) -> float:
    d = max(0.0, mouth_to_ear_ms)
    i_d = 0.024 * d + (0.11 * (d - 177.3) if d > 177.3 else 0.0)
    ppl = max(0.0, loss_pct)
    ie_eff = ie + (95.0 - ie) * ppl / (ppl / burst_r + bpl)
    return 93.2 - i_d - ie_eff


def r_to_mos(r: float) -> float:
    if r <= 0:
        return 1.0
    if r >= 100:
        return 4.5
    return 1.0 + 0.035 * r + 7e-6 * r * (r - 60.0) * (100.0 - r)


def voice_mos(owd_samples_ms: Sequence[float], loss_pct: float, jitter_ms: float,
              codec_delay_ms: float = 25.0) -> dict:
    """MOS for the audio leg of a call.

    A receiver-side adaptive jitter buffer is modelled: its depth is max(20 ms, 2.5 x jitter)
    and packets arriving later than (median OWD + buffer) are counted as lost ("late loss").
    """
    if not len(owd_samples_ms):
        return {"mos": None, "r": None, "late_loss_pct": None, "mouth_to_ear_ms": None}
    a = np.asarray(owd_samples_ms, dtype=float)
    med = float(np.median(a))
    jbuf = max(20.0, 2.5 * jitter_ms)
    late = float((a > med + jbuf).mean() * 100.0)
    m2e = med + jbuf + codec_delay_ms
    eff_loss = min(100.0, loss_pct + late * (1 - loss_pct / 100.0))
    r = emodel_r(m2e, eff_loss)
    return {"mos": r_to_mos(r), "r": r, "late_loss_pct": late, "mouth_to_ear_ms": m2e}


# ---------------------------------------------------------------- gaming
def gaming_mos(rtt_p95_ms: float | None, jitter_ms: float, loss_pct: float) -> float | None:
    """1..5 interactivity score. 5 at <=40 ms RTT, 1 at >=250 ms; jitter and loss penalised."""
    if rtt_p95_ms is None or math.isnan(rtt_p95_ms):
        return None
    base = 5.0 - 4.0 * min(1.0, max(0.0, (rtt_p95_ms - 40.0) / 210.0))
    base -= 0.03 * max(0.0, jitter_ms - 5.0)
    base -= 0.35 * max(0.0, loss_pct)
    return float(min(5.0, max(1.0, base)))


# ---------------------------------------------------------------- streaming video
def bitrate_quality(mbps: float, top_mbps: float = 16.0) -> float:
    """0..1 logarithmic utility of a video bitrate (diminishing returns)."""
    if mbps <= 0:
        return 0.0
    return float(min(1.0, math.log(1 + mbps / 0.3) / math.log(1 + top_mbps / 0.3)))


def streaming_mos(avg_bitrate_mbps: float | None, rebuffer_ratio: float, stalls_per_min: float,
                  switches_per_min: float = 0.0) -> float | None:
    if avg_bitrate_mbps is None:
        return None
    q = 1.0 + 3.8 * bitrate_quality(avg_bitrate_mbps)
    q -= 12.0 * min(0.25, rebuffer_ratio)  # 10% stalled time costs 1.2 MOS
    q -= 0.5 * min(4.0, stalls_per_min)
    q -= 0.1 * min(5.0, switches_per_min)
    return float(min(5.0, max(1.0, q)))


def qoe_lin(bitrates_mbps: Sequence[float], rebuffer_s: float, mu: float = 4.3) -> float:
    """QoE_lin from Yin et al. (SIGCOMM'15) / Pensieve: sum q - mu*rebuffer - sum |dq|."""
    if not bitrates_mbps:
        return 0.0
    b = list(bitrates_mbps)
    smooth = sum(abs(b[i + 1] - b[i]) for i in range(len(b) - 1))
    return float(sum(b) - mu * rebuffer_s - smooth)


# ---------------------------------------------------------------- web
def web_mos(plt_s: float | None) -> float | None:
    if plt_s is None:
        return None
    return float(min(5.0, max(1.0, 5.0 - 1.4 * math.log(max(plt_s, 0.5) / 0.5))))


# ---------------------------------------------------------------- bulk
def bulk_mos(throughput_mbps: float, fair_share_mbps: float) -> float:
    if fair_share_mbps <= 0:
        return 5.0
    return float(1.0 + 4.0 * min(1.0, throughput_mbps / fair_share_mbps))


# ---------------------------------------------------------------- fairness
def jain(values: Iterable[float]) -> float | None:
    x = np.asarray([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    if x.size == 0:
        return None
    s2 = float((x ** 2).sum())
    if s2 == 0:
        return 1.0
    return float(x.sum() ** 2 / (x.size * s2))


def qoe_fairness(mos_values: Iterable[float], low: float = 1.0, high: float = 5.0) -> float | None:
    x = np.asarray([v for v in mos_values if v is not None and not math.isnan(v)], dtype=float)
    if x.size == 0:
        return None
    return float(1.0 - 2.0 * x.std() / (high - low))


def max_min_fair_shares(demands: dict[str, float], capacity: float) -> dict[str, float]:
    """Max-min fair allocation with demand caps (water-filling). Infinite demand allowed."""
    alloc = {k: 0.0 for k in demands}
    active = {k for k, d in demands.items() if d > 0}
    remaining = capacity
    while active and remaining > 1e-9:
        share = remaining / len(active)
        done = {k for k in active if demands[k] - alloc[k] <= share}
        if not done:
            for k in active:
                alloc[k] += share
            remaining = 0.0
            break
        for k in done:
            remaining -= demands[k] - alloc[k]
            alloc[k] = demands[k]
        active -= done
    return alloc
