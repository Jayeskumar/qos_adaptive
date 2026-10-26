"""Run the benchmark matrix on the real-kernel Linux testbed (sequential: one testbed).

    sudo python -m flowpilot.bench.kernel_bench --out results/kernel_bench.json

Schedulers that need qdiscs the running kernel lacks (fq_codel, cake) are skipped
automatically. Results use the same format as the simulator benchmark and can be imported
into the database with `python -m flowpilot.db.importer`.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..testbed.netns import qdisc_supported
from .kernel_runner import run_kernel
from .scenarios import SCENARIO_ORDER

DEFAULT = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_rl"]


def available(schedulers: list[str]) -> list[str]:
    fq = qdisc_supported("fq_codel")
    cake = qdisc_supported("cake")
    out = []
    for s in schedulers:
        if s == "fq_codel" and not fq:
            continue
        if s.startswith("cake") and not cake:
            continue
        out.append(s)
    return out


def compact(r: dict) -> dict:
    series = []
    for snap in r.pop("series", []):
        row = {"t": snap["t"], "cap_down": snap["capacity_down"], "cap_up": snap["capacity_up"],
               "shaper_down": snap.get("shaper_down"), "shaper_up": snap.get("shaper_up"),
               "isp_q_down": snap["isp_queue_ms_down"], "isp_q_up": snap["isp_queue_ms_up"], "apps": {}}
        for a in snap["apps"]:
            row["apps"][a["app_id"]] = {k: a.get(k) for k in (
                "app_type", "latency_ms", "latency_p95_ms", "latency_p99_ms", "loss_pct", "mos", "kbps_down",
                "kbps_up", "bitrate_mbps", "buffer_s", "rebuffer_s", "plt_s", "throughput_mbps",
                "video_kbps_down", "video_kbps_up")}
        series.append(row)
    r["series"] = series
    r["decisions"] = r.get("decisions", [])[-150:]
    return r


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="+", default=SCENARIO_ORDER)
    ap.add_argument("--schedulers", nargs="+", default=DEFAULT)
    ap.add_argument("--seeds", nargs="+", type=int, default=[1])
    ap.add_argument("--out", default="results/kernel_bench.json")
    a = ap.parse_args()
    scheds = available(a.schedulers)
    print("schedulers on this kernel:", scheds, flush=True)
    out_path = Path(a.out)
    results = []
    if out_path.exists():  # resume
        results = json.loads(out_path.read_text()).get("results", [])
    done = {(r["scenario"], r["scheduler"], r["seed"]) for r in results}
    t0 = time.time()
    for seed in a.seeds:
        for sc in a.scenarios:
            for sch in scheds:
                if (sc, sch, seed) in done:
                    continue
                t1 = time.time()
                try:
                    r = compact(run_kernel(sc, sch, seed))
                except Exception as exc:  # pragma: no cover - record and continue
                    import traceback

                    r = {"mode": "kernel", "scenario": sc, "scheduler": sch, "seed": seed, "error": repr(exc),
                         "trace": traceback.format_exc()}
                results.append(r)
                s = r.get("summary", {})
                print(f"[{time.time() - t0:5.0f}s] {sc:16s} {sch:14s} seed={seed} ({time.time() - t1:.0f}s) "
                      + (f"ERROR {r['error']}" if "error" in r else
                         f"game_p99={s.get('game_rtt_p99_ms') or 0:.0f} call_p99={s.get('call_owd_p99_ms') or 0:.0f} "
                         f"mos={s.get('mean_mos') or 0:.2f} acc={(r.get('flow_accuracy') or {}).get('accuracy')}"),
                      flush=True)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                "mode": "kernel", "results": results}))
    print(f"saved {len(results)} runs to {out_path}")


if __name__ == "__main__":
    main()
