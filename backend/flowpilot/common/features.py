"""Behavioural flow features, computed identically in the simulator and on the real router.

FlowPilot never looks at payloads or port numbers when it classifies traffic: modern
applications are encrypted (TLS 1.3 / QUIC) and almost everything runs on 443. Instead it
uses the *shape* of a flow, which encryption does not hide:

  packet sizes per direction, packet and byte rates, inter-arrival regularity,
  ON/OFF burstiness at 100 ms resolution, up/down asymmetry, and how many parallel
  connections the device keeps to the same server.

Statistics are kept over a sliding window of `WINDOW_S` seconds in 100 ms bins so the
classifier can track flows that change behaviour (e.g. a video player that is filling its
buffer looks like a download for the first seconds, then turns ON/OFF).
"""
from __future__ import annotations

import math
from typing import Iterator

BIN_S = 0.1
NBINS = 50
WINDOW_S = BIN_S * NBINS
SMALL = 200
LARGE = 1200

FEATURE_NAMES = [
    "is_udp", "log_age_s",
    "log_pps_up", "log_pps_down", "log_kbps_up", "log_kbps_down",
    "mean_size_up", "mean_size_down", "std_size_up", "std_size_down",
    "frac_small_up", "frac_large_down", "frac_large_up", "frac_small_down",
    "iat_cv_up", "iat_cv_down", "log_iat_mean_up_ms", "log_iat_mean_down_ms",
    "burst_cv_up", "burst_cv_down", "on_frac_up", "on_frac_down",
    "log_ratio_up_down", "log_peak_kbps_down", "log_host_flows", "log_host_same_dst",
]

# per-bin accumulator layout
_P, _B, _S2, _SM, _LG, _IS, _IS2, _IN = range(8)
_W = 8


class _Dir:
    __slots__ = ("bins", "cur", "last_ts", "pkts", "bytes")

    def __init__(self) -> None:
        self.bins = [0.0] * (NBINS * _W)
        self.cur = -1  # absolute bin index of the most recent bin written
        self.last_ts = -1.0
        self.pkts = 0
        self.bytes = 0

    def add(self, size: int, now: float) -> None:
        b = int(now / BIN_S)
        bins = self.bins
        if b != self.cur:
            if self.cur < 0 or b - self.cur >= NBINS:
                for i in range(NBINS * _W):
                    bins[i] = 0.0
            else:
                for j in range(self.cur + 1, b + 1):
                    o = (j % NBINS) * _W
                    for k in range(_W):
                        bins[o + k] = 0.0
            self.cur = b
        o = (b % NBINS) * _W
        bins[o] += 1.0
        bins[o + _B] += size
        bins[o + _S2] += size * size
        if size <= SMALL:
            bins[o + _SM] += 1.0
        elif size >= LARGE:
            bins[o + _LG] += 1.0
        if self.last_ts >= 0:
            iat = now - self.last_ts
            bins[o + _IS] += iat
            bins[o + _IS2] += iat * iat
            bins[o + _IN] += 1.0
        self.last_ts = now
        self.pkts += 1
        self.bytes += size


class FlowStats:
    __slots__ = ("key", "proto", "lan_ip", "wan_ip", "first_ts", "last_ts", "up", "down", "meta")

    def __init__(self, key: tuple, proto: str, lan_ip: str, wan_ip: str, now: float) -> None:
        self.key = key
        self.proto = proto
        self.lan_ip = lan_ip
        self.wan_ip = wan_ip
        self.first_ts = now
        self.last_ts = now
        self.up = _Dir()
        self.down = _Dir()
        self.meta: dict = {}

    @property
    def pkts(self) -> int:
        return self.up.pkts + self.down.pkts


def _dir_stats(d: _Dir, now: float, first_ts: float) -> dict:
    """Aggregate the bins of one direction that overlap the window and the flow lifetime."""
    end_bin = int(now / BIN_S)
    start_bin = max(end_bin - NBINS + 1, int(first_ts / BIN_S))
    n_bins = max(1, end_bin - start_bin + 1)
    tot = [0.0] * _W
    per_bin_bytes = []
    if d.cur >= 0:
        bins = d.bins
        for j in range(start_bin, end_bin + 1):
            if j > d.cur or d.cur - j >= NBINS:
                per_bin_bytes.append(0.0)
                continue
            o = (j % NBINS) * _W
            for k in range(_W):
                tot[k] += bins[o + k]
            per_bin_bytes.append(bins[o + _B])
    else:
        per_bin_bytes = [0.0] * n_bins
    span = max(BIN_S, min(now - first_ts, WINDOW_S))
    p = tot[_P]
    mean_sz = tot[_B] / p if p else 0.0
    var_sz = max(0.0, tot[_S2] / p - mean_sz * mean_sz) if p else 0.0
    n_iat = tot[_IN]
    iat_mean = tot[_IS] / n_iat if n_iat else span
    iat_var = max(0.0, tot[_IS2] / n_iat - iat_mean * iat_mean) if n_iat else 0.0
    iat_cv = math.sqrt(iat_var) / iat_mean if iat_mean > 0 else 0.0
    m = sum(per_bin_bytes) / len(per_bin_bytes) if per_bin_bytes else 0.0
    if m > 0:
        v = sum((x - m) ** 2 for x in per_bin_bytes) / len(per_bin_bytes)
        burst_cv = math.sqrt(v) / m
    else:
        burst_cv = 0.0
    on = sum(1 for x in per_bin_bytes if x > 0) / len(per_bin_bytes) if per_bin_bytes else 0.0
    peak = max(per_bin_bytes) if per_bin_bytes else 0.0
    return {
        "pps": p / span, "kbps": tot[_B] * 8 / 1000 / span, "bytes": tot[_B],
        "mean_size": mean_sz, "std_size": math.sqrt(var_sz),
        "frac_small": tot[_SM] / p if p else 0.0, "frac_large": tot[_LG] / p if p else 0.0,
        "iat_cv": iat_cv, "iat_mean_ms": iat_mean * 1000,
        "burst_cv": burst_cv, "on_frac": on, "peak_kbps": peak * 8 / 1000 / BIN_S,
    }


def feature_vector(fs: FlowStats, now: float, host_flows: int, host_same_dst: int) -> tuple[list[float], dict]:
    u = _dir_stats(fs.up, now, fs.first_ts)
    d = _dir_stats(fs.down, now, fs.first_ts)
    age = now - fs.first_ts
    vec = [
        1.0 if fs.proto == "udp" else 0.0,
        math.log1p(age),
        math.log1p(u["pps"]), math.log1p(d["pps"]),
        math.log1p(u["kbps"]), math.log1p(d["kbps"]),
        u["mean_size"], d["mean_size"], u["std_size"], d["std_size"],
        u["frac_small"], d["frac_large"], u["frac_large"], d["frac_small"],
        min(u["iat_cv"], 20.0), min(d["iat_cv"], 20.0),
        math.log1p(u["iat_mean_ms"]), math.log1p(d["iat_mean_ms"]),
        min(u["burst_cv"], 20.0), min(d["burst_cv"], 20.0), u["on_frac"], d["on_frac"],
        math.log((u["bytes"] + 1.0) / (d["bytes"] + 1.0)),
        math.log1p(d["peak_kbps"]),
        math.log1p(host_flows), math.log1p(host_same_dst),
    ]
    info = {"age_s": age, "kbps_up": u["kbps"], "kbps_down": d["kbps"], "pps_up": u["pps"],
            "pps_down": d["pps"], "mean_size_up": u["mean_size"], "mean_size_down": d["mean_size"],
            "burst_cv_down": d["burst_cv"], "on_frac_down": d["on_frac"], "iat_cv_up": u["iat_cv"],
            "iat_cv_down": d["iat_cv"], "host_same_dst": host_same_dst, "peak_kbps_down": d["peak_kbps"],
            "peak_kbps_up": u["peak_kbps"]}
    return vec, info


class FlowMonitor:
    """Per-flow behavioural statistics at the router (both directions)."""

    def __init__(self, idle_timeout: float = 10.0) -> None:
        self.flows: dict[tuple, FlowStats] = {}
        self.idle_timeout = idle_timeout

    def observe(self, key: tuple, proto: str, lan_ip: str, wan_ip: str, up: bool, size: int, now: float) -> None:
        fs = self.flows.get(key)
        if fs is None:
            fs = self.flows[key] = FlowStats(key, proto, lan_ip, wan_ip, now)
        fs.last_ts = now
        (fs.up if up else fs.down).add(size, now)

    def expire(self, now: float) -> list[tuple]:
        dead = [k for k, f in self.flows.items() if now - f.last_ts > self.idle_timeout]
        for k in dead:
            del self.flows[k]
        return dead

    def host_counts(self, now: float, recent: float = 2.0) -> tuple[dict, dict]:
        per_host: dict[str, int] = {}
        per_dst: dict[tuple[str, str], int] = {}
        for f in self.flows.values():
            if now - f.last_ts <= recent:
                per_host[f.lan_ip] = per_host.get(f.lan_ip, 0) + 1
                k = (f.lan_ip, f.wan_ip)
                per_dst[k] = per_dst.get(k, 0) + 1
        return per_host, per_dst

    def iter_features(self, now: float, min_pkts: int = 6, active_within: float = 2.0
                      ) -> Iterator[tuple[FlowStats, list[float], dict]]:
        per_host, per_dst = self.host_counts(now)
        for fs in list(self.flows.values()):
            if fs.pkts < min_pkts or now - fs.last_ts > active_within:
                continue
            vec, info = feature_vector(fs, now, per_host.get(fs.lan_ip, 1),
                                       per_dst.get((fs.lan_ip, fs.wan_ip), 1))
            yield fs, vec, info
