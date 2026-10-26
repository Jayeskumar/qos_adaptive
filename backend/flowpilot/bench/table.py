"""Print a compact comparison table from a benchmark results file."""
from __future__ import annotations

import json
import sys
from collections import defaultdict

import numpy as np

COLS = [("game_rtt_p99_ms", "game p99"), ("call_owd_p99_ms", "call p99"), ("call_mos", "callMOS"),
        ("stream_bitrate_mbps", "Mbps 4K"), ("stream_rebuffer_pct", "rebuf%"), ("web_plt_p50_s", "PLT s"),
        ("bulk_mbps", "bulk"), ("upload_mbps", "upl"), ("utilization_down_pct", "util%"),
        ("jain_bulk_devices", "jainDev"), ("qoe_fairness", "QoE-F"), ("mean_mos", "meanMOS"), ("min_mos", "minMOS")]


def load(path: str) -> dict:
    res = json.load(open(path))["results"]
    agg: dict = defaultdict(lambda: defaultdict(list))
    for r in res:
        if "error" in r:
            print("ERROR", r["scenario"], r["scheduler"], r["error"])
            continue
        for k, _ in COLS:
            v = r["summary"].get(k)
            if v is not None:
                agg[(r["scenario"], r["scheduler"])][k].append(v)
    return agg


def main() -> None:
    agg = load(sys.argv[1])
    scen = []
    for (sc, _sch) in agg:
        if sc not in scen:
            scen.append(sc)
    for sc in scen:
        print(f"\n== {sc}")
        print(f"{'scheduler':15s}" + "".join(f"{h:>9s}" for _, h in COLS))
        for (s2, sch), m in agg.items():
            if s2 != sc:
                continue
            row = f"{sch:15s}"
            for k, _ in COLS:
                v = m.get(k)
                row += f"{np.mean(v):9.2f}" if v else f"{'-':>9s}"
            print(row)


if __name__ == "__main__":
    main()
