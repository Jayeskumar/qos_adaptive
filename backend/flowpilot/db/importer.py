"""Import benchmark result files (simulator or kernel) into the database.

    python -m flowpilot.db.importer results/sim_bench.json results/kernel_bench.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..common.jsonio import read_json
from .models import Decision, Run, Sample
from .store import Store, _clean

KEEP_APP_KEYS = ("app_type", "latency_ms", "latency_p95_ms", "latency_p99_ms", "loss_pct", "mos", "kbps_down",
                 "kbps_up", "bitrate_mbps", "buffer_s", "rebuffer_s", "plt_s", "throughput_mbps",
                 "video_kbps_down", "video_kbps_up")


def _row(snap: dict) -> dict:
    if "apps" in snap and isinstance(snap["apps"], dict):  # compact bench format
        return snap
    apps = {a["app_id"]: {k: a.get(k) for k in KEEP_APP_KEYS} for a in snap.get("apps", [])}
    return {"t": snap["t"], "cap_down": snap.get("capacity_down"), "cap_up": snap.get("capacity_up"),
            "shaper_down": snap.get("shaper_down"), "shaper_up": snap.get("shaper_up"),
            "isp_q_down": snap.get("isp_queue_ms_down"), "isp_q_up": snap.get("isp_queue_ms_up"), "apps": apps}


def import_file(store: Store, path: str | Path, replace: bool = True) -> int:
    data = read_json(path)
    results = data["results"] if isinstance(data, dict) and "results" in data else (
        data if isinstance(data, list) else [data])
    mode = results[0].get("mode", "sim") if results else "sim"
    if replace:
        store.delete_benchmarks(mode)
    n = 0
    with store.Session() as s:
        for r in results:
            if "error" in r:
                continue
            run = Run(kind="benchmark", mode=r.get("mode", "sim"), scenario=r["scenario"], scheduler=r["scheduler"],
                      seed=int(r.get("seed", 1)), status="done", label=r.get("label", r["scheduler"]),
                      duration_s=r.get("duration"), summary=_clean(r["summary"]),
                      meta=_clean({"source": str(path), "timeline": r.get("timeline", [])[:200],
                                   "flow_accuracy": r.get("flow_accuracy"),
                                   "classifier_stats": r.get("classifier_stats"),
                                   "controller": r.get("controller")}))
            s.add(run)
            s.flush()
            for snap in r.get("series", []):
                row = _row(snap)
                s.add(Sample(run_id=run.id, side="a", t=float(row["t"]), data=_clean(row)))
            for d in r.get("decisions", [])[-150:]:
                s.add(Decision(run_id=run.id, t=float(d.get("t", 0)), kind=d.get("kind", ""),
                               message=d.get("message", ""), details=_clean(d.get("details"))))
            n += 1
        s.commit()
    return n


def main() -> None:
    store = Store()
    for p in sys.argv[1:]:
        print(f"{p}: imported {import_file(store, p)} runs into {store.backend}")


if __name__ == "__main__":
    main()
