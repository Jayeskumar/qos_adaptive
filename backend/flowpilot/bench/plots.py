"""Generate the report figures from benchmark / model results.

    python -m flowpilot.bench.plots --sim results/sim_bench.json --kernel results/kernel_bench.json

Visual conventions (docs/REPORT.md uses them throughout): one colour per router policy,
fixed across every figure (FlowPilot = blue), thin 2 px lines, solid hairline grids, log
scales for latency (a standard router reaches seconds, FlowPilot stays in tens of ms),
direct labels instead of legends where there are few series.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.lines  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, LogNorm  # noqa: E402

from .runner import SCHEDULER_LABELS  # noqa: E402
from .scenarios import SCENARIOS, SCENARIO_ORDER  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
REST = "#c9c7bf"  # de-emphasised bars

COLOR = {  # fixed entity -> colour (reference palette order)
    "flowpilot": "#2a78d6", "fifo": "#eb6834", "cake": "#1baf7a", "cake_autorate": "#eda100",
    "static_qos": "#e87ba4", "fq_codel": "#008300", "flowpilot_rl": "#6250d6", "flowpilot_fq": "#e34948",
}
ORDER = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_fq", "flowpilot_rl"]
SHORT = {"fifo": "Standard\n(FIFO)", "static_qos": "Static\nQoS", "fq_codel": "fq_codel", "cake": "CAKE",
         "cake_autorate": "CAKE +\nautorate", "flowpilot": "FlowPilot", "flowpilot_fq": "FlowPilot\n+fq leaves",
         "flowpilot_rl": "FlowPilot\n+RL"}
BLUE_SEQ = LinearSegmentedColormap.from_list("seq", ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"])


def style() -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK, "axes.titleweight": "bold",
        "axes.titlesize": 11, "axes.labelsize": 9.5, "xtick.color": MUTED, "ytick.color": MUTED,
        "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "axes.grid": True, "grid.color": GRID,
        "grid.linewidth": 0.6, "grid.linestyle": "-", "axes.spines.top": False, "axes.spines.right": False,
        "font.family": "DejaVu Sans", "legend.frameon": False, "legend.fontsize": 8.5, "lines.linewidth": 1.6,
        "text.color": INK,
    })


def load(path: str | Path | None) -> list[dict]:
    from ..common.jsonio import read_json, resolve

    if not path:
        return []
    path = resolve(path)
    if not Path(path).exists():
        return []
    d = read_json(path)
    return [r for r in d.get("results", d if isinstance(d, list) else []) if "error" not in r]


def agg(results: list[dict], key: str) -> dict:
    out: dict = defaultdict(list)
    for r in results:
        v = r["summary"].get(key)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            out[(r["scenario"], r["scheduler"])].append(v)
    return {k: (float(np.mean(v)), float(np.min(v)), float(np.max(v)), len(v)) for k, v in out.items()}


def first(results: list[dict], scenario: str, scheduler: str, seed: int | None = None):
    for r in results:
        if r["scenario"] == scenario and r["scheduler"] == scheduler and (seed is None or r["seed"] == seed):
            return r
    return None


def series_of(r: dict, app_id: str, field: str) -> tuple[np.ndarray, np.ndarray]:
    t, v = [], []
    for row in r.get("series", []):
        a = row["apps"].get(app_id) if isinstance(row["apps"], dict) else None
        if a and a.get(field) is not None:
            t.append(row["t"])
            v.append(a[field])
    return np.array(t), np.array(v, dtype=float)


_PENDING: dict = {}


def end_label(ax, x, y, text, color, dy=0.0) -> None:
    """Queue a direct label at the end of a line; `place_labels(ax)` lays them out."""
    if len(x):
        yy = [v for v in y if v == v]
        if not yy:
            return
        _PENDING.setdefault(id(ax), (ax, []))[1].append((x[-1], yy[-1], text, color))
        ax.plot([x[-1]], [yy[-1]], "o", ms=4.5, color=color, mec=SURFACE, mew=1.5, zorder=5)


def place_labels(fig, min_gap_pt: float = 11.0) -> None:
    """Spread end labels vertically so they never overlap (keeps each next to its line end)."""
    fig.canvas.draw()
    for ax, items in _PENDING.values():
        if not items:
            continue
        pts = []
        for x, y, text, color in items:
            _, py = ax.transData.transform((x, y))
            pts.append([py * 72 / fig.dpi, x, y, text, color])
        pts.sort(key=lambda p: p[0])
        pos = [p[0] for p in pts]
        for _ in range(50):
            moved = False
            for i in range(1, len(pos)):
                if pos[i] - pos[i - 1] < min_gap_pt:
                    mid = (pos[i] + pos[i - 1]) / 2
                    pos[i - 1] = mid - min_gap_pt / 2
                    pos[i] = mid + min_gap_pt / 2
                    moved = True
            if not moved:
                break
        for (py, x, y, text, color), target in zip(pts, pos):
            ax.annotate(text, (x, y), xytext=(8, target - py), textcoords="offset points", color=INK2,
                        fontsize=8.5, va="center", fontweight="bold")
    _PENDING.clear()


def log_ms_axis(ax, lo: float, hi: float, ticks: list) -> None:
    from matplotlib.ticker import FixedLocator, NullFormatter, NullLocator

    ax.set_yscale("log")
    ax.set_ylim(lo, hi)
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.set_yticklabels([f"{t:g}" for t in ticks])
    ax.yaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_minor_formatter(NullFormatter())


def mark_event(ax, t: float, text: str) -> None:
    ax.axvline(t, color=MUTED, lw=0.8, zorder=1)
    ax.annotate(text, (t, 1.0), xycoords=("data", "axes fraction"), xytext=(4, -10), textcoords="offset points",
                fontsize=8, color=INK2, va="top")


# ============================================================================ figures
def fig_latency_timeline(sim: list[dict], out: Path, scenario: str = "evening_rush") -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), sharey=True)
    for ax, (app, title) in zip(axes, (("game", "Online game: round-trip time (p95 per second)"),
                                        ("call", "Teams call: one-way delay (p95 per second)"))):
        for sch in ("fifo", "cake", "flowpilot"):
            r = first(sim, scenario, sch, 1)
            if r is None:
                continue
            t, v = series_of(r, app, "latency_p95_ms")
            ax.plot(t, v, color=COLOR[sch], lw=1.6)
            end_label(ax, t, v, SCHEDULER_LABELS[sch].replace(" (AI)", ""), COLOR[sch])
        log_ms_axis(ax, 15, 1000, [20, 30, 50, 100, 200, 500, 1000])
        ax.set_xlabel("time (s)")
        ax.set_title(title, loc="left")
        mark_event(ax, 20, "Steam update starts\n(8 TCP connections)")
        ax.set_xlim(0, (SCENARIOS[scenario].duration) * 1.28)
    axes[0].set_ylabel("milliseconds (log scale)")
    fig.tight_layout()
    place_labels(fig)
    fig.savefig(out / "latency_timeline.png", dpi=160)
    plt.close(fig)


def fig_congestion(sim: list[dict], out: Path, scenario: str = "isp_congestion", fname: str = "congestion_timeline.png",
                   title_extra: str = "") -> None:
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10.5, 5.6), sharex=True, gridspec_kw={"height_ratios": [1, 1.25]})
    base = first(sim, scenario, "flowpilot", 1) or first(sim, scenario, "cake", 1)
    if base is None:
        plt.close(fig)
        return
    t = np.array([row["t"] for row in base["series"]])
    cap = np.array([row["cap_down"] / 1e6 for row in base["series"]])
    a1.step(t, cap, where="post", color=INK, lw=1.2)
    end_label(a1, t, cap, "ISP capacity", INK)
    for sch in ("cake", "cake_autorate", "flowpilot"):
        r = first(sim, scenario, sch, 1)
        if r is None:
            continue
        tt = np.array([row["t"] for row in r["series"]])
        sh = [row.get("shaper_down") for row in r["series"]]
        if sch == "cake" or all(x is None for x in sh):
            sh = np.full(len(tt), 0.95 * SCENARIOS[scenario].link.down_mbps)
        else:
            sh = np.array([x / 1e6 if x else np.nan for x in sh])
        a1.plot(tt, sh, color=COLOR[sch], lw=1.5)
        end_label(a1, tt, sh, "shaper: " + SCHEDULER_LABELS[sch].replace(" (AI)", "").replace("SQM ", ""),
                  COLOR[sch])
    a1.set_ylabel("Mb/s")
    a1.set_ylim(0, max(cap) * 1.15)
    a1.set_title(f"ISP capacity vs the router's shaper{title_extra}", loc="left")
    for sch in ("cake", "cake_autorate", "flowpilot"):
        r = first(sim, scenario, sch, 1)
        if r is None:
            continue
        tt, v = series_of(r, "game", "latency_p95_ms")
        a2.plot(tt, v, color=COLOR[sch], lw=1.5)
        end_label(a2, tt, v, SCHEDULER_LABELS[sch].replace(" (AI)", "").replace("SQM ", ""), COLOR[sch])
    log_ms_axis(a2, 15, 2000, [20, 50, 100, 200, 500, 1000, 2000])
    a2.set_ylabel("game RTT p95 (ms, log)")
    a2.set_xlabel("time (s)")
    a2.set_title("What the gamer feels", loc="left")
    for ev in SCENARIOS[scenario].caps:
        if ev.label:
            for ax in (a1, a2):
                ax.axvline(ev.t, color=MUTED, lw=0.8, zorder=1)
    a2.set_xlim(0, SCENARIOS[scenario].duration * 1.25)
    fig.tight_layout()
    place_labels(fig)
    fig.savefig(out / fname, dpi=160)
    plt.close(fig)


def _matrix(ax, results, key, scheds, title, fmt="{:.0f}", vmin=20, vmax=1200, log=True, cmap=BLUE_SEQ,
            scenarios=None):
    m = agg(results, key)
    scenarios = scenarios or [s for s in SCENARIO_ORDER if any((s, x) in m for x in scheds)]
    data = np.full((len(scenarios), len(scheds)), np.nan)
    for i, sc in enumerate(scenarios):
        for j, sch in enumerate(scheds):
            if (sc, sch) in m:
                data[i, j] = m[(sc, sch)][0]
    norm = LogNorm(vmin=vmin, vmax=vmax) if log else None
    im = ax.imshow(np.ma.masked_invalid(data), cmap=cmap, norm=norm, aspect="auto",
                   vmin=None if log else vmin, vmax=None if log else vmax)
    ax.set_xticks(range(len(scheds)))
    ax.set_xticklabels([SHORT[s] for s in scheds], fontsize=8)
    ax.set_yticks(range(len(scenarios)))
    ax.set_yticklabels([SCENARIOS[s].title for s in scenarios], fontsize=8.5)
    ax.grid(False)
    for i in range(len(scenarios)):
        for j in range(len(scheds)):
            v = data[i, j]
            if np.isnan(v):
                ax.text(j, i, "n/a", ha="center", va="center", fontsize=7.5, color=MUTED)
                continue
            frac = (math.log(max(v, vmin)) - math.log(vmin)) / (math.log(vmax) - math.log(vmin)) if log else \
                (v - vmin) / (vmax - vmin)
            ax.text(j, i, fmt.format(v), ha="center", va="center", fontsize=8.5,
                    color="#ffffff" if frac > 0.55 else INK, fontweight="bold" if sch_is_fp(scheds[j]) else None)
    ax.set_title(title, loc="left")
    for sp in ax.spines.values():
        sp.set_visible(False)
    return im


def sch_is_fp(s: str) -> bool:
    return s.startswith("flowpilot")


def fig_matrix(results: list[dict], out: Path, fname: str, scheds: list[str], mode_title: str) -> None:
    scheds = [s for s in scheds if any(r["scheduler"] == s for r in results)]
    if not scheds:
        return
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.2), gridspec_kw={"wspace": 0.04})
    scen = [s for s in SCENARIO_ORDER if any(r["scenario"] == s for r in results)]
    _matrix(axes[0], results, "game_rtt_p99_ms", scheds, f"Game RTT p99 (ms) - {mode_title}", scenarios=scen)
    im = _matrix(axes[1], results, "call_owd_p99_ms", scheds, f"Video-call one-way delay p99 (ms) - {mode_title}",
                 vmin=15, vmax=1200, scenarios=scen)
    axes[1].set_yticklabels([])
    cb = fig.colorbar(im, ax=axes, fraction=0.025, pad=0.02)
    cb.ax.tick_params(labelsize=8, colors=MUTED)
    cb.outline.set_visible(False)
    fig.savefig(out / fname, dpi=160, bbox_inches="tight")
    plt.close(fig)


def fig_mos(results: list[dict], out: Path, fname: str, scheds: list[str], title: str) -> None:
    scheds = [s for s in scheds if any(r["scheduler"] == s for r in results)]
    m = agg(results, "interactive_mos")
    mn = agg(results, "min_interactive_mos")
    scenarios = [s for s in SCENARIO_ORDER if any((s, x) in m for x in scheds)]
    n = len(scenarios)
    cols = 3
    rows = int(math.ceil(n / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(12.5, 3.1 * rows), sharey=True)
    axes = np.atleast_1d(axes).ravel()
    for ax, sc in zip(axes, scenarios):
        xs = np.arange(len(scheds))
        vals = [m.get((sc, s), (np.nan,))[0] for s in scheds]
        mins = [mn.get((sc, s), (np.nan,))[0] for s in scheds]
        cols_ = [COLOR[s] if sch_is_fp(s) else REST for s in scheds]
        ax.bar(xs, vals, width=0.62, color=cols_, edgecolor=SURFACE, linewidth=2, zorder=3)
        ax.scatter(xs, mins, marker="_", s=180, color=INK2, zorder=4, linewidths=1.6)
        best_base = max((v for s, v in zip(scheds, vals) if not sch_is_fp(s) and not np.isnan(v)), default=None)
        for x, s, v in zip(xs, scheds, vals):
            if not np.isnan(v) and (sch_is_fp(s) or v == best_base):
                ax.text(x, v + 0.08, f"{v:.2f}", ha="center", va="bottom", fontsize=8, color=INK,
                        fontweight="bold" if sch_is_fp(s) else None)
        ax.set_xticks(xs)
        ax.set_xticklabels([SHORT[s] for s in scheds], fontsize=7.2)
        ax.set_ylim(1, 5.3)
        ax.set_title(SCENARIOS[sc].title, loc="left", fontsize=10)
        ax.grid(axis="x", visible=False)
    for ax in axes[len(scenarios):]:
        ax.set_visible(False)
    axes[0].set_ylabel("MOS (1-5)")
    fig.suptitle(title, x=0.01, ha="left", fontsize=11.5, fontweight="bold", color=INK)
    fig.text(0.01, 0.005, "Bars: mean MOS of the interactive applications (game, call, video, web). Tick: the worst "
                          "of them. FlowPilot variants in colour, baselines in grey.", fontsize=8, color=INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    fig.savefig(out / fname, dpi=160)
    plt.close(fig)


def fig_alloc(sim: list[dict], out: Path, scenario: str = "evening_rush") -> None:
    apps = [("game", "Game"), ("call", "Teams call"), ("web", "Web"), ("netflix", "Netflix"), ("steam", "Steam")]
    acol = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 3.6), sharey=True)
    for ax, sch in zip(axes, ("cake", "flowpilot")):
        r = first(sim, scenario, sch, 1)
        if r is None:
            continue
        t = np.array([row["t"] for row in r["series"]])
        ys = []
        for aid, _ in apps:
            ys.append(np.array([(row["apps"].get(aid) or {}).get("kbps_down") or 0.0 for row in r["series"]]) / 1000)
        ax.stackplot(t, *ys, colors=acol, edgecolor=SURFACE, linewidth=0.6, zorder=3)
        cap = np.array([row["cap_down"] / 1e6 for row in r["series"]])
        ax.plot(t, cap, color=INK, lw=1.0)
        ax.set_title(f"{SCHEDULER_LABELS[sch]}: downlink by application", loc="left")
        ax.set_xlabel("time (s)")
        ax.set_xlim(0, t[-1])
        ax.set_ylim(0, 56)
    axes[0].set_ylabel("Mb/s")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in acol]
    axes[1].legend(handles, [n for _, n in apps], loc="upper left", ncol=5, fontsize=8, bbox_to_anchor=(0, -0.18))
    fig.tight_layout()
    fig.savefig(out / "allocation_stack.png", dpi=160)
    plt.close(fig)


def fig_kernel_vs_sim(sim: list[dict], ker: list[dict], out: Path) -> None:
    """Does the digital twin predict the real kernel? One point per (scenario, policy) run on both."""
    scheds = [s for s in ORDER if any(r["scheduler"] == s for r in ker)]
    if not scheds:
        return
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.5, 5.0))
    errs, derr = [], []
    for k, marker in (("game_rtt_p99_ms", "o"), ("call_owd_p99_ms", "D")):
        ms, mk = agg(sim, k), agg(ker, k)
        for s in scheds:
            pts = [(ms[(sc, s)][0], mk[(sc, s)][0]) for sc in SCENARIO_ORDER if (sc, s) in ms and (sc, s) in mk]
            if pts:
                x, y = zip(*pts)
                ax1.scatter(x, y, s=64 if marker == "o" else 52, marker=marker, color=COLOR[s], edgecolor=SURFACE,
                            linewidth=1.5, zorder=3)
                errs += [abs(b - a) / a for a, b in pts]
    lim = (20, 1500)
    ax1.plot(lim, lim, color=MUTED, lw=1, ls="--", zorder=2)
    ax1.fill_between(lim, [v / 1.25 for v in lim], [v * 1.25 for v in lim], color=GRID, alpha=0.6, lw=0, zorder=1)
    ax1.set_xscale("log")
    ax1.set_yscale("log")
    for ax_ in (ax1.xaxis, ax1.yaxis):
        ax_.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
        ax_.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax1.set_xticks([20, 50, 100, 200, 500, 1000])
    ax1.set_yticks([20, 50, 100, 200, 500, 1000])
    ax1.set_xlim(*lim)
    ax1.set_ylim(*lim)
    ax1.set_xlabel("digital twin (ms, mean of 3 seeds)")
    ax1.set_ylabel("real Linux kernel (ms)")
    ax1.set_title("99th-percentile latency: game RTT (●) and call one-way delay (◆)", loc="left", fontsize=10)
    ax1.text(0.03, 0.95, f"median deviation {np.median(errs) * 100:.0f} %\nshaded: within ±25 %", transform=ax1.transAxes,
             va="top", fontsize=8.5, color=INK2)
    ms, mk = agg(sim, "interactive_mos"), agg(ker, "interactive_mos")
    for s in scheds:
        pts = [(ms[(sc, s)][0], mk[(sc, s)][0]) for sc in SCENARIO_ORDER if (sc, s) in ms and (sc, s) in mk]
        if pts:
            x, y = zip(*pts)
            ax2.scatter(x, y, s=64, color=COLOR[s], edgecolor=SURFACE, linewidth=1.5, zorder=3)
            derr += [abs(b - a) for a, b in pts]
    ax2.plot((1, 5), (1, 5), color=MUTED, lw=1, ls="--", zorder=2)
    ax2.set_xlim(1, 5)
    ax2.set_ylim(1, 5)
    ax2.set_xlabel("digital twin (MOS, mean of 3 seeds)")
    ax2.set_ylabel("real Linux kernel (MOS)")
    ax2.set_title("Interactive QoE (mean MOS of game, call, video, web)", loc="left", fontsize=10)
    ax2.text(0.03, 0.95, f"mean |difference| {np.mean(derr):.2f} MOS", transform=ax2.transAxes, va="top",
             fontsize=8.5, color=INK2)
    handles = [matplotlib.lines.Line2D([], [], ls="", marker="o", ms=8, color=COLOR[s], mec=SURFACE,
                                       label=SCHEDULER_LABELS.get(s, s)) for s in scheds]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False, fontsize=9,
               bbox_to_anchor=(0.5, 0.0))
    fig.suptitle("Digital twin vs real Linux kernel: every scenario × router run on both", x=0.01, ha="left",
                 fontsize=11.5, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0.06, 1, 0.97))
    fig.savefig(out / "sim_vs_kernel.png", dpi=160)
    plt.close(fig)


def fig_classifier(card: dict, out: Path) -> None:
    if not card:
        return
    labels = ["Gaming", "Video call", "Streaming", "Web", "Bulk", "Upload"]
    cm = np.array(card["test"]["confusion"], dtype=float)
    norm = cm / cm.sum(1, keepdims=True)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.0), gridspec_kw={"width_ratios": [1.05, 1.0, 1.1]})
    ax = axes[0]
    ax.imshow(norm, cmap=BLUE_SEQ, vmin=0, vmax=1)
    ax.grid(False)
    ax.set_xticks(range(6))
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.set_yticks(range(6))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("predicted")
    ax.set_ylabel("true application")
    for i in range(6):
        for j in range(6):
            if cm[i, j] > 0:
                ax.text(j, i, f"{norm[i, j] * 100:.1f}" if norm[i, j] < 0.995 else "100", ha="center", va="center",
                        fontsize=7.5, color="#ffffff" if norm[i, j] > 0.55 else INK)
    for sp in ax.spines.values():
        sp.set_visible(False)
    acc = card["test"]["accuracy"] * 100
    ax.set_title(f"Confusion matrix (% of row), accuracy {acc:.1f} %", loc="left", fontsize=10)
    ax = axes[1]
    ba = card["test"]["by_age"]
    xs = np.arange(len(ba))
    ys = [b["accuracy"] * 100 for b in ba]
    ax.plot(xs, ys, color=COLOR["flowpilot"], lw=2, marker="o", ms=6, mec=SURFACE, mew=1.5)
    ax.set_xticks(xs)
    ax.set_xticklabels([f"<{b['age_hi']:.0f}s" if b["age_hi"] else ">16s" for b in ba], fontsize=8)
    ax.set_ylim(95, 100.5)
    ax.set_ylabel("accuracy (%)")
    ax.set_xlabel("flow age when classified")
    for x, y in zip(xs, ys):
        ax.text(x, y + 0.25, f"{y:.1f}", ha="center", fontsize=7.5, color=INK2)
    ax.set_title("How early can FlowPilot tell?", loc="left", fontsize=10)
    ax = axes[2]
    imp = card["importance"][:10][::-1]
    ax.barh([d["feature"] for d in imp], [d["importance"] * 100 for d in imp], color=COLOR["flowpilot"], height=0.6,
            edgecolor=SURFACE, lw=1.5, zorder=3)
    ax.set_xlabel("accuracy drop when shuffled (pp)")
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="y", visible=False)
    ax.set_title("Most informative behavioural features", loc="left", fontsize=10)
    fig.tight_layout()
    fig.savefig(out / "classifier.png", dpi=160)
    plt.close(fig)


def fig_rl(rep: dict, out: Path) -> None:
    if not rep or "eval" not in rep:
        return
    fig, axes = plt.subplots(1, 4, figsize=(16, 3.6), gridspec_kw={"width_ratios": [1.25, 1, 1, 1]})
    tr = rep.get("training", {})
    curve = tr.get("eval_curve", [])
    ax = axes[0]
    if curve:
        x = np.array([c["transitions"] for c in curve]) / 1e6
        u = np.array([c["utilization"] for c in curve]) * 100
        o = np.array([c["frac_over_20ms"] for c in curve]) * 100
        ref = tr.get("eval_reference", {})
        ax.plot(x, u, color=COLOR["flowpilot_rl"], lw=2, marker="o", ms=4.5, mec=SURFACE, mew=1.2)
        ax.plot(x, o, color=COLOR["fifo"], lw=2, marker="s", ms=4.5, mec=SURFACE, mew=1.2)
        if ref:
            ax.axhline(ref["utilization"] * 100, color=COLOR["flowpilot_rl"], lw=1, alpha=0.45)
            ax.axhline(ref["frac_over_20ms"] * 100, color=COLOR["fifo"], lw=1, alpha=0.45)
        end_label(ax, x, u, "utilisation %", COLOR["flowpilot_rl"])
        end_label(ax, x, o, "% time > 20 ms", COLOR["fifo"])
        ax.set_ylim(0, 100)
        ax.set_xlim(0, x[-1] * 1.45)
        ax.set_xlabel("training transitions (millions)")
        ax.set_ylabel("percent")
        ax.text(0.01, 0.03, "thin lines: heuristic autorate on the same 16 held-out links", transform=ax.transAxes,
                fontsize=7.5, color=INK2)
    ax.set_title("Learning curve (held-out links)", loc="left", fontsize=10)
    ev = {e["policy"]: e for e in rep["eval"]}
    kinds = [("fixed", "Fixed line +\ncongestion"), ("fwa", "5G FWA\n(fades)"), ("lte", "LTE\nregimes")]
    pols = [("heuristic", "Heuristic autorate", COLOR["cake_autorate"]), ("rl", "DQN residual agent",
                                                                          COLOR["flowpilot_rl"])]
    panels = ((axes[1], "utilization", "Utilisation, % (higher is better)", "{:.0f}", 100.0),
              (axes[2], "delay_p95_ms", "Queueing p95, ms (lower is better)", "{:.0f}", 1.0),
              (axes[3], "frac_over_20ms", "Time > 20 ms, % (lower is better)", "{:.0f}", 100.0))
    for ax, metric, title, fmt, scale in panels:
        xs = np.arange(len(kinds))
        w = 0.36
        for i, (p, name, c) in enumerate(pols):
            if p not in ev:
                continue
            vals = [ev[p]["by_kind"].get(k, {}).get(metric, np.nan) * scale for k, _ in kinds]
            ax.bar(xs + (i - 0.5) * (w + 0.02), vals, width=w, color=c, edgecolor=SURFACE, lw=2, label=name, zorder=3)
            for x, v in zip(xs + (i - 0.5) * (w + 0.02), vals):
                if v == v:
                    ax.text(x, v, fmt.format(v), ha="center", va="bottom", fontsize=7.5, color=INK2)
        ax.set_xticks(xs)
        ax.set_xticklabels([n for _, n in kinds], fontsize=8)
        ax.set_title(title, loc="left", fontsize=10)
        ax.grid(axis="x", visible=False)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in pols]
    fig.legend(handles, [n for _, n, _ in pols], loc="lower center", ncol=2, fontsize=8.5,
               bbox_to_anchor=(0.62, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    place_labels(fig)
    fig.savefig(out / "rl_autorate.png", dpi=160)
    plt.close(fig)


def fig_fairness(sim: list[dict], out: Path) -> None:
    scheds = [s for s in ("fifo", "fq_codel", "cake", "cake_autorate", "flowpilot")
              if first(sim, "download_hog", s)]
    if not scheds:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 3.4))
    xs = np.arange(len(scheds))
    w = 0.36
    pc, tab, nf = [], [], []
    for s in scheds:
        apps = first(sim, "download_hog", s, 1)["summary"]["apps"]
        pc.append(apps["torrent"]["avg_throughput_mbps"])
        tab.append(apps["update"]["avg_throughput_mbps"])
        nf.append(apps["netflix"]["avg_bitrate_mbps"] or 0)
    ax = axes[0]
    ax.bar(xs - w / 2 - 0.01, pc, width=w, color="#2a78d6", edgecolor=SURFACE, lw=2, label="PC: 16 connections", zorder=3)
    ax.bar(xs + w / 2 + 0.01, tab, width=w, color="#eb6834", edgecolor=SURFACE, lw=2, label="Tablet: 1 connection",
           zorder=3)
    ax.set_xticks(xs)
    ax.set_xticklabels([SHORT[s] for s in scheds], fontsize=8)
    ax.set_ylabel("average download (Mb/s)")
    ax.legend(loc="upper right", fontsize=8)
    ax.set_title("Per-device fairness between two downloads", loc="left", fontsize=10)
    ax.grid(axis="x", visible=False)
    ax = axes[1]
    ax.bar(xs, nf, width=0.55, color=[COLOR[s] if sch_is_fp(s) else REST for s in scheds], edgecolor=SURFACE, lw=2,
           zorder=3)
    for x, v in zip(xs, nf):
        ax.text(x, v + 0.2, f"{v:.1f}", ha="center", fontsize=8, color=INK2)
    ax.set_xticks(xs)
    ax.set_xticklabels([SHORT[s] for s in scheds], fontsize=8)
    ax.set_ylabel("Netflix average bitrate (Mb/s)")
    ax.set_title("...while the 4K stream keeps its quality", loc="left", fontsize=10)
    ax.grid(axis="x", visible=False)
    fig.tight_layout()
    fig.savefig(out / "fairness.png", dpi=160)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", default="results/sim_bench.json")
    ap.add_argument("--kernel", default="results/kernel_bench.json")
    ap.add_argument("--classifier", default="data/classifier_report.json")
    ap.add_argument("--rl", default="data/rl_report.json")
    ap.add_argument("--out", default="../docs/figures")
    a = ap.parse_args()
    style()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sim = load(a.sim)
    ker = load(a.kernel)
    if sim:
        fig_latency_timeline(sim, out)
        fig_congestion(sim, out)
        fig_congestion(sim, out, "fwa_fading", "fwa_timeline.png", " (5G FWA)")
        fig_matrix(sim, out, "p99_matrix_sim.png", ORDER, "simulator")
        fig_mos(sim, out, "qoe_sim.png", [s for s in ORDER if s != "flowpilot_fq"], "Quality of experience (simulator)")
        fig_alloc(sim, out)
        fig_fairness(sim, out)
    if ker:
        fig_matrix(ker, out, "p99_matrix_kernel.png", ORDER, "real Linux testbed")
        fig_mos(ker, out, "qoe_kernel.png", ORDER, "Quality of experience (real Linux testbed)")
        if sim:
            fig_kernel_vs_sim(sim, ker, out)
    p = Path(a.classifier)
    if p.exists():
        fig_classifier(json.loads(p.read_text()), out)
    p = Path(a.rl)
    if p.exists():
        fig_rl(json.loads(p.read_text()), out)
    print("figures:", sorted(x.name for x in out.glob("*.png")))


if __name__ == "__main__":
    main()
