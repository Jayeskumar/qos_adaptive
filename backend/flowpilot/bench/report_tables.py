"""Emit every markdown results table in docs/REPORT.md straight from the result files.

    python -m flowpilot.bench.report_tables --sim results/sim_bench.json.gz --kernel results/kernel_bench.json
    python -m flowpilot.bench.report_tables --section loss      # one section only
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np

from .runner import SCHEDULER_LABELS
from .scenarios import SCENARIOS, SCENARIO_ORDER

ORDER = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_fq", "flowpilot_rl"]
FIXED_SQM = ["static_qos", "fq_codel", "cake"]
FP_FAMILY = ("flowpilot", "flowpilot_fq", "flowpilot_rl")
SHORT_SCHED = {"fifo": "FIFO", "static_qos": "static QoS", "fq_codel": "fq_codel", "cake": "CAKE",
               "cake_autorate": "CAKE + autorate", "flowpilot": "FlowPilot", "flowpilot_fq": "FlowPilot + fq",
               "flowpilot_rl": "FlowPilot + RL"}
SHORT_SCEN = {"evening_rush": "Evening rush", "upload_storm": "Upload storm", "wfh_same_laptop": "WFH sync",
              "isp_congestion": "ISP congestion", "download_hog": "Download hog", "fwa_fading": "5G FWA"}

# key -> (column header, format, which direction is better)
METRICS = {
    "game_rtt_p50_ms": ("Game RTT p50 (ms)", "{:.0f}", "low"),
    "game_rtt_p99_ms": ("Game RTT p99 (ms)", "{:.0f}", "low"),
    "game_loss_pct": ("Game loss (%)", "{:.2f}", "low"),
    "call_owd_p99_ms": ("Call OWD p99 (ms)", "{:.0f}", "low"),
    "call_loss_pct": ("Call loss (%)", "{:.2f}", "low"),
    "call_freeze_pct": ("Call frames late (%)", "{:.1f}", "low"),
    "slo_ok_pct": ("Within SLO (% s)", "{:.0f}", "high"),
    "call_mos": ("Call MOS", "{:.2f}", "high"),
    "stream_bitrate_mbps": ("Netflix (Mb/s)", "{:.1f}", "high"),
    "stream_rebuffer_pct": ("Rebuffering (%)", "{:.1f}", "low"),
    "web_plt_p50_s": ("Web PLT p50 (s)", "{:.2f}", "low"),
    "interactive_mos": ("Interactive MOS", "{:.2f}", "high"),
    "qoe_fairness": ("QoE fairness F", "{:.2f}", "high"),
    "jain_bulk_devices": ("Jain (devices)", "{:.2f}", "high"),
    "bulk_mbps": ("Download (Mb/s)", "{:.1f}", "high"),
    "upload_mbps": ("Upload (Mb/s)", "{:.1f}", "high"),
    "utilization_down_pct": ("Utilisation (%)", "{:.0f}", "high"),
}
LATENCY_LOSS = ["game_rtt_p50_ms", "game_rtt_p99_ms", "game_loss_pct", "call_owd_p99_ms", "call_loss_pct",
                "call_freeze_pct", "slo_ok_pct"]
QOE_THROUGHPUT = ["call_mos", "stream_bitrate_mbps", "stream_rebuffer_pct", "web_plt_p50_s", "interactive_mos",
                  "qoe_fairness", "jain_bulk_devices", "bulk_mbps", "upload_mbps", "utilization_down_pct"]
DROP_IF_ZERO = {"bulk_mbps", "upload_mbps"}


def load(path) -> list:
    from ..common.jsonio import read_json, resolve

    if not path:
        return []
    path = resolve(path)
    if not Path(path).exists():
        return []
    return [r for r in read_json(path)["results"] if "error" not in r]


def aggregate(res: list) -> dict:
    agg: dict = defaultdict(lambda: defaultdict(list))
    for r in res:
        cell = agg[(r["scenario"], r["scheduler"])]
        for k in METRICS:
            v = r["summary"].get(k)
            if v is not None:
                cell[k].append(v)
    return agg


def mean(agg: dict, sc: str, s: str, k: str) -> Optional[float]:
    v = agg.get((sc, s), {}).get(k)
    return float(np.mean(v)) if v else None


def fmt(k: str, v: Optional[float]) -> str:
    return "–" if v is None else METRICS[k][1].format(v)


def best_str(k: str, vals: list) -> Optional[str]:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return fmt(k, (min if METRICS[k][2] == "low" else max)(vals))


def bold_if(txt: str, best: Optional[str]) -> str:
    return f"**{txt}**" if best is not None and txt == best else txt


def name(s: str) -> str:
    n = SCHEDULER_LABELS.get(s, s)
    return f"**{n}**" if s.startswith("flowpilot") else n


# ----------------------------------------------------------------------------- per-scenario tables
def table(agg: dict, sc: str, keys: list, note: str) -> str:
    scheds = [s for s in ORDER if (sc, s) in agg]
    cols = []
    for k in keys:
        vals = [mean(agg, sc, s, k) for s in scheds]
        if all(v is None for v in vals) or (k in DROP_IF_ZERO and not any(vals)):
            continue
        cols.append(k)
    best = {k: best_str(k, [mean(agg, sc, s, k) for s in scheds]) for k in cols}
    lines = ["| Router | " + " | ".join(METRICS[k][0] for k in cols) + " |",
             "|---|" + "|".join("---:" for _ in cols) + "|"]
    for s in scheds:
        cells = [bold_if(fmt(k, mean(agg, sc, s, k)), best[k]) for k in cols]
        lines.append(f"| {name(s)} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + f"\n\n<sub>{note}; best value per column in bold.</sub>\n"


def appendix(agg: dict, kind: str, note: str) -> str:
    out = []
    for sc in SCENARIO_ORDER:
        if not any(k[0] == sc for k in agg):
            continue
        out.append(f"### {SCENARIOS[sc].title} — {kind}\n")
        out.append(f"*{SCENARIOS[sc].description}*\n")
        out.append("**Latency and loss**\n")
        out.append(table(agg, sc, LATENCY_LOSS, note))
        out.append("**Experience, fairness and throughput**\n")
        out.append(table(agg, sc, QOE_THROUGHPUT, note))
    return "\n".join(out)


# ----------------------------------------------------------------------------- condensed tables
HEADLINE = [("game_rtt_p99_ms", "game RTT p99 (ms)"), ("call_owd_p99_ms", "call one-way delay p99 (ms)"),
            ("interactive_mos", "interactive MOS (1-5)"), ("slo_ok_pct", "seconds within SLO (%)")]


def headline(agg: dict) -> str:
    lines = ["| Scenario | Metric | Standard router | Best fixed-rate SQM | CAKE + autorate | FlowPilot |",
             "|---|---|---:|---:|---:|---:|"]
    for sc in SCENARIO_ORDER:
        for k, label in HEADLINE:
            fifo = mean(agg, sc, "fifo", k)
            if fifo is None:
                continue
            fixed = {s: mean(agg, sc, s, k) for s in FIXED_SQM if mean(agg, sc, s, k) is not None}
            pick = (min if METRICS[k][2] == "low" else max)(fixed, key=fixed.get)
            vals = [fifo, fixed[pick], mean(agg, sc, "cake_autorate", k), mean(agg, sc, "flowpilot", k)]
            best = best_str(k, vals)
            cells = [bold_if(fmt(k, v), best) for v in vals]
            cells[1] = f"{cells[1]} ({SHORT_SCHED[pick]})"
            lines.append(f"| {SHORT_SCEN[sc]} | {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n\n<sub>Digital twin, mean of 3 seeds. Best fixed-rate SQM = the best of static QoS, " \
                              "fq_codel and CAKE for that metric (named). Best of the four columns in bold.</sub>\n"


def by_policy(agg: dict, rows: list, scheds: list, note: str) -> str:
    """rows: (scenario, metric key, row label)."""
    scheds = [s for s in scheds if any(k[1] == s for k in agg)]
    lines = ["| Scenario | Metric | " + " | ".join(SHORT_SCHED[s] for s in scheds) + " |",
             "|---|---|" + "|".join("---:" for _ in scheds) + "|"]
    for sc, k, label in rows:
        vals = [mean(agg, sc, s, k) for s in scheds]
        if all(v is None for v in vals):
            continue
        best = best_str(k, vals)
        lines.append(f"| {SHORT_SCEN[sc]} | {label} | " + " | ".join(bold_if(fmt(k, v), best) for v in vals) + " |")
    return "\n".join(lines) + f"\n\n<sub>{note}; best per row in bold.</sub>\n"


LOSS_ROWS = [("call_loss_pct", "call packets lost (%)"), ("call_freeze_pct", "call video frames late (%)"),
             ("stream_rebuffer_pct", "Netflix time rebuffering (%)")]
SIM_POLICIES = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot"]


def loss(agg: dict, scheds=SIM_POLICIES, note="digital twin, mean of 3 seeds") -> str:
    return by_policy(agg, [(sc, k, lb) for sc in SCENARIO_ORDER for k, lb in LOSS_ROWS], scheds, note)


def fairness(agg: dict, scheds=SIM_POLICIES, note="digital twin, mean of 3 seeds") -> str:
    rows = [(sc, "qoe_fairness", "QoE fairness F across all apps") for sc in SCENARIO_ORDER]
    rows.append(("download_hog", "jain_bulk_devices", "Jain index, throughput of the 2 downloading devices"))
    return by_policy(agg, rows, scheds, note)


KERNEL_POLICIES = ["fifo", "static_qos", "flowpilot", "flowpilot_rl"]
KERNEL_ROWS = [("game_rtt_p99_ms", "game RTT p99 (ms)"), ("call_owd_p99_ms", "call one-way delay p99 (ms)"),
               ("call_freeze_pct", "call video frames late (%)"), ("web_plt_p50_s", "web page load p50 (s)"),
               ("stream_bitrate_mbps", "Netflix bitrate (Mb/s)"), ("interactive_mos", "interactive MOS (1-5)"),
               ("slo_ok_pct", "seconds within SLO (%)"), ("qoe_fairness", "QoE fairness F")]


def kernel(kagg: dict, sagg: dict, runs_note: str = "one run per cell") -> str:
    scheds = [s for s in KERNEL_POLICIES if any(k[1] == s for k in kagg)]
    lines = ["| Scenario | Metric | " + " | ".join(SHORT_SCHED[s] for s in scheds) + " | *FlowPilot in the twin* |",
             "|---|---|" + "|".join("---:" for _ in scheds) + "|---:|"]
    for sc in SCENARIO_ORDER:
        if not any(k[0] == sc for k in kagg):
            continue
        for k, label in KERNEL_ROWS:
            vals = [mean(kagg, sc, s, k) for s in scheds]
            if all(v is None for v in vals):
                continue
            best = best_str(k, vals)
            twin = mean(sagg, sc, "flowpilot", k)
            lines.append(f"| {SHORT_SCEN[sc]} | {label} | " + " | ".join(bold_if(fmt(k, v), best) for v in vals)
                         + f" | *{fmt(k, twin)}* |")
    return "\n".join(lines) + f"\n\n<sub>Real Linux kernel testbed, {runs_note}; best kernel value per row in " \
                              "bold; last column: the digital twin's FlowPilot (mean of 3 seeds) for comparison.</sub>\n"


def _range(vals: list, k: str) -> str:
    vals = [v for v in vals if v is not None]
    lo, hi = min(vals), max(vals)
    return f"{fmt(k, lo)} – {fmt(k, hi)}" if fmt(k, lo) != fmt(k, hi) else fmt(k, lo)


def summary_rows(agg: dict, where: str) -> str:
    """Executive-summary rows: standard router vs FlowPilot ranges over all scenarios."""
    out = []
    for k, label, unit in (("game_rtt_p99_ms", "Game RTT p99", " ms"), ("call_owd_p99_ms", "Call one-way delay p99", " ms"),
                           ("slo_ok_pct", "Game and call seconds within SLO", " %"),
                           ("interactive_mos", "Interactive QoE, MOS 1-5", "")):
        scs = [sc for sc in SCENARIO_ORDER if mean(agg, sc, "fifo", k) is not None and mean(agg, sc, "flowpilot", k) is not None]
        if not scs:
            continue
        f = [mean(agg, sc, "fifo", k) for sc in scs]
        p = [mean(agg, sc, "flowpilot", k) for sc in scs]
        extra = ""
        if METRICS[k][2] == "low":
            cuts = [(1 - b / a) * 100 for a, b in zip(f, p)]
            extra = f" (−{min(cuts):.0f} to −{max(cuts):.0f} %)" if round(min(cuts)) != round(max(cuts)) \
                else f" (−{min(cuts):.0f} %)"
        out.append(f"| {label} ({where}) | {_range(f, k)}{unit} | **{_range(p, k)}{unit}**{extra} |")
    return "\n".join(out)


def accuracy(res: list, scheds=FP_FAMILY) -> dict:
    """Ground-truth labelling accuracy over every flow the controller labelled (incl. flows that ended)."""
    runs = [r for r in res if r["scheduler"] in scheds and (r.get("flow_accuracy") or {}).get("flows")]
    tot = sum(r["flow_accuracy"]["flows"] for r in runs)
    ok = sum(round(r["flow_accuracy"]["accuracy"] * r["flow_accuracy"]["flows"]) for r in runs)
    byte = [r["flow_accuracy"]["byte_accuracy"] for r in runs if r["flow_accuracy"].get("byte_accuracy") is not None]
    conf: dict = {}
    for r in runs:
        for truth, row in (r["flow_accuracy"].get("confusion") or {}).items():
            for lab, n in row.items():
                conf.setdefault(truth, {}).setdefault(lab, 0)
                conf[truth][lab] += n
    return {"runs": len(runs), "flows": tot, "correct": ok, "accuracy": ok / tot if tot else None,
            "byte_accuracy_mean": float(np.mean(byte)) if byte else None,
            "byte_accuracy_min": float(np.min(byte)) if byte else None, "confusion": conf}


APP_ORDER = ["gaming", "video_call", "streaming", "web", "bulk", "upload"]
APP_NAME = {"gaming": "game", "video_call": "video call", "streaming": "video streaming", "web": "web",
            "bulk": "download", "upload": "upload / backup"}


def accuracy_md(acc: dict) -> str:
    conf = acc["confusion"]
    seen = {l for row in conf.values() for l in row} | set(conf)
    labels = [a for a in APP_ORDER if a in seen] + sorted(seen - set(APP_ORDER))
    lines = ["| true \\ labelled as | " + " | ".join(APP_NAME.get(l, l) for l in labels) + " | flows | recall |",
             "|---|" + "---:|" * (len(labels) + 2)]
    for t in [a for a in labels if a in conf]:
        row = conf[t]
        n = sum(row.values())
        lines.append(f"| {APP_NAME.get(t, t)} | " + " | ".join(str(row.get(l, 0)) for l in labels)
                     + f" | {n} | {row.get(t, 0) / n * 100:.0f} % |")
    return "\n".join(lines)


SECTIONS = ("headline", "loss", "fairness", "kernel", "kernel-loss", "summary", "accuracy", "appendix-sim",
            "appendix-kernel")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", default="results/sim_bench.json.gz")
    ap.add_argument("--kernel", default="results/kernel_bench.json")
    ap.add_argument("--section", choices=SECTIONS + ("all",), default="all")
    a = ap.parse_args()
    sim, ker = load(a.sim), load(a.kernel)
    sagg, kagg = aggregate(sim), aggregate(ker)
    want = SECTIONS if a.section == "all" else (a.section,)
    for sec in want:
        print(f"\n<!-- {sec} -->\n")
        if sec == "headline":
            print(headline(sagg))
        elif sec == "loss":
            print(loss(sagg))
        elif sec == "fairness":
            print(fairness(sagg))
        elif sec == "kernel" and ker:
            print(kernel(kagg, sagg))
        elif sec == "kernel-loss" and ker:
            print(loss(kagg, KERNEL_POLICIES, "real Linux kernel, one run per cell"))
            print(fairness(kagg, KERNEL_POLICIES, "real Linux kernel, one run per cell"))
        elif sec == "summary":
            print(summary_rows(sagg, "digital twin"))
            if ker:
                print(summary_rows(kagg, "real kernel"))
        elif sec == "accuracy":
            for label, res in (("digital twin", sim), ("real kernel", ker)):
                if res:
                    acc = accuracy(res)
                    print(f"{label}: {acc['correct']}/{acc['flows']} flows = {acc['accuracy'] * 100:.1f} %, "
                          f"bytes {acc['byte_accuracy_mean'] * 100:.1f} % (min {acc['byte_accuracy_min'] * 100:.1f} %) "
                          f"over {acc['runs']} runs\n")
                    print(accuracy_md(acc) + "\n")
        elif sec == "appendix-sim":
            print(appendix(sagg, "digital twin", "mean of 3 seeds"))
        elif sec == "appendix-kernel" and ker:
            print(appendix(kagg, "real Linux kernel", "one run on the Linux testbed"))


if __name__ == "__main__":
    main()
