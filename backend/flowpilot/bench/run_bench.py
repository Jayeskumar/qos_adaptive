"""Run the simulator benchmark matrix in parallel and save results.

    python -m flowpilot.bench.run_bench --seeds 1 2 3 --out results/sim_bench.json
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")
from pathlib import Path

from .runner import run_sim
from .scenarios import SCENARIO_ORDER

DEFAULT_SCHEDULERS = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_fq"]


def _job(args):
    scenario, scheduler, seed = args
    try:
        r = run_sim(scenario, scheduler, seed=seed, keep_series=True)
        # keep the per-second series compact: only what the report plots need
        series = []
        for snap in r.pop("series"):
            row = {"t": snap["t"], "cap_down": snap["capacity_down"], "cap_up": snap["capacity_up"],
                   "shaper_down": snap.get("shaper_down"), "shaper_up": snap.get("shaper_up"),
                   "isp_q_down": snap["isp_queue_ms_down"], "isp_q_up": snap["isp_queue_ms_up"], "apps": {}}
            for a in snap["apps"]:
                row["apps"][a["app_id"]] = {k: a.get(k) for k in (
                    "app_type", "latency_ms", "latency_p95_ms", "latency_p99_ms", "loss_pct", "mos",
                    "kbps_down", "kbps_up", "bitrate_mbps", "buffer_s", "rebuffer_s", "plt_s",
                    "throughput_mbps", "video_kbps_down", "video_kbps_up")}
            series.append(row)
        r["series"] = series
        r["decisions"] = r.get("decisions", [])[-120:]
        return r
    except Exception as exc:  # pragma: no cover - reported in the output
        import traceback

        return {"scenario": scenario, "scheduler": scheduler, "seed": seed, "error": repr(exc),
                "trace": traceback.format_exc()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="+", default=SCENARIO_ORDER)
    ap.add_argument("--schedulers", nargs="+", default=DEFAULT_SCHEDULERS)
    ap.add_argument("--seeds", nargs="+", type=int, default=[1])
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--out", default="results/sim_bench.json")
    a = ap.parse_args()
    jobs = [(sc, sch, sd) for sc in a.scenarios for sch in a.schedulers for sd in a.seeds]
    t0 = time.time()
    results = []
    with mp.Pool(a.workers or mp.cpu_count()) as pool:
        for i, r in enumerate(pool.imap_unordered(_job, jobs)):
            results.append(r)
            s = r.get("summary", {})
            print(f"[{i + 1}/{len(jobs)} {time.time() - t0:.0f}s] {r['scenario']:16s} {r['scheduler']:14s} seed={r['seed']} "
                  + (f"ERROR {r['error']}" if "error" in r else
                     f"game_p99={s.get('game_rtt_p99_ms') or 0:.0f} call_p99={s.get('call_owd_p99_ms') or 0:.0f} "
                     f"mos={s.get('mean_mos') or 0:.2f}"), flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}))
    print(f"saved {len(results)} runs to {a.out} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
