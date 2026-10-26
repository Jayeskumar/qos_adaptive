"""Run a scenario in the simulator under a given scheduler and compute all metrics."""
from __future__ import annotations

import time
from typing import Callable, Optional

from ..common import metrics as M
from ..common.classes import App
from ..sim.world import HomeWorld
from .scenarios import build_sim, get

SCHEDULERS = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_rl"]
SCHEDULER_LABELS = {
    "fifo": "Standard router (FIFO)",
    "static_qos": "Static QoS (port rules)",
    "fq_codel": "SQM fq_codel",
    "cake": "SQM CAKE",
    "cake_autorate": "CAKE + autorate",
    "flowpilot": "FlowPilot (AI)",
    "flowpilot_fq": "FlowPilot + fq_codel leaves",
    "flowpilot_rl": "FlowPilot + RL autorate",
}


def make_qos(name: str, intent: str = "balanced", classifier=None):
    from ..controller.classifier import FlowClassifier
    from ..controller.sim_qos import FlowPilotQoS
    from ..sim.baselines import make_baseline

    if name.startswith("flowpilot"):
        clf = classifier or FlowClassifier()
        if name == "flowpilot":
            return FlowPilotQoS(clf, intent=intent)
        if name == "flowpilot_fq":
            return FlowPilotQoS(clf, intent=intent, leaf="fq_codel", name=name, label=SCHEDULER_LABELS[name])
        if name == "flowpilot_rl":
            from ..ml.rl.policy import RLAutorate

            return FlowPilotQoS(clf, autorate=RLAutorate(), intent=intent, name=name, label=SCHEDULER_LABELS[name])
        if name == "flowpilot_rules":
            from ..controller.classifier import FlowClassifier as FC

            c = FC(model_path="/nonexistent")
            return FlowPilotQoS(c, intent=intent, name=name, label="FlowPilot (rule classifier)")
    return make_baseline(name)


def collect(w: HomeWorld, t: float, dt: float) -> dict:
    apps = [a.snapshot(t, dt) for a in w.apps.values() if a.active or (a.t_stop and t - a.t_stop < dt)]
    snap = {
        "t": round(t, 2),
        "capacity_down": w.capacity_down, "capacity_up": w.capacity_up,
        "isp_queue_ms_down": w.isp_down.qdisc.backlog_bytes * 8 / w.capacity_down * 1000,
        "isp_queue_ms_up": w.isp_up.qdisc.backlog_bytes * 8 / w.capacity_up * 1000,
        "apps": apps,
    }
    st = w.qos.status()
    if "autorate" in st:
        snap["shaper_down"] = st["autorate"]["down"]["rate"]
        snap["shaper_up"] = st["autorate"]["up"]["rate"]
    elif "shaper_down" in st:
        snap["shaper_down"] = st["shaper_down"]
        snap["shaper_up"] = st["shaper_up"]
    if "classes" in st:
        snap["classes"] = st["classes"]
    return snap


def summarize(w: HomeWorld, series: list[dict]) -> dict:
    return summarize_apps({aid: a.summary() for aid, a in w.apps.items()}, series)


def summarize_apps(apps: dict, series: list[dict]) -> dict:
    """Headline metrics from per-app summaries + the per-second series (sim or kernel)."""
    out: dict = {"apps": apps}
    # ---- headline per application type
    by_type: dict[str, list] = {}
    for a in apps.values():
        by_type.setdefault(a["app_type"], []).append(a)
    g = by_type.get(App.GAMING, [])
    c = by_type.get(App.VIDEO_CALL, [])
    s = by_type.get(App.STREAMING, [])
    web = by_type.get(App.WEB, [])
    bulk = by_type.get(App.BULK, []) + by_type.get(App.UPLOAD, [])
    if g:
        out["game_rtt_p50_ms"] = g[0]["rtt_ms"]["p50"]
        out["game_rtt_p95_ms"] = g[0]["rtt_ms"]["p95"]
        out["game_rtt_p99_ms"] = g[0]["rtt_ms"]["p99"]
        out["game_added_p99_ms"] = g[0]["added_latency_p99_ms"]
        out["game_jitter_ms"] = g[0]["jitter_ms"]
        out["game_loss_pct"] = g[0]["loss_pct"]
        out["game_mos"] = g[0]["mos"]
    if c:
        out["call_owd_p95_ms"] = c[0]["owd_p95_ms"]
        out["call_owd_p99_ms"] = c[0]["owd_p99_ms"]
        out["call_added_p99_ms"] = c[0]["added_latency_p99_ms"]
        out["call_loss_pct"] = c[0]["loss_pct"]
        out["call_mos"] = c[0]["mos"]
        out["call_freeze_pct"] = c[0]["freeze_pct"]
        out["call_video_kbps_up"] = c[0]["video_kbps_up"]
        out["call_video_kbps_down"] = c[0]["video_kbps_down"]
    if s:
        out["stream_bitrate_mbps"] = s[0]["avg_bitrate_mbps"]
        out["stream_rebuffer_pct"] = s[0]["rebuffer_ratio"] * 100
        out["stream_stalls"] = s[0]["stalls"]
        out["stream_mos"] = s[0]["mos"]
    if web:
        out["web_plt_p50_s"] = web[0]["plt_p50_s"]
        out["web_plt_p95_s"] = web[0]["plt_p95_s"]
        out["web_mos"] = web[0]["mos"]
    if bulk:
        out["bulk_mbps"] = sum(b["avg_throughput_mbps"] for b in bulk if not b.get("upload"))
        out["upload_mbps"] = sum(b["avg_throughput_mbps"] for b in bulk if b.get("upload"))
    # ---- link utilisation while a capacity-seeking app is active
    used = cap = 0.0
    for snap in series:
        if any(a["app_type"] in (App.BULK,) and a["active"] for a in snap["apps"]):
            used += sum(a["kbps_down"] for a in snap["apps"]) * 1000
            cap += snap["capacity_down"]
    out["utilization_down_pct"] = used / cap * 100 if cap else None
    # ---- SLO compliance per second
    ok = tot = 0
    for snap in series:
        for a in snap["apps"]:
            if not a["active"]:
                continue
            if a["app_type"] == App.GAMING and a.get("latency_p95_ms") is not None:
                tot += 1
                ok += (a["latency_p95_ms"] - a["base_rtt_ms"] <= 30 and a["loss_pct"] <= 1.0)
            elif a["app_type"] == App.VIDEO_CALL and a.get("latency_p95_ms") is not None:
                tot += 1
                ok += (a["latency_p95_ms"] - a["base_owd_ms"] <= 50 and a["loss_pct"] <= 2.0)
    out["slo_ok_pct"] = ok / tot * 100 if tot else None
    # ---- fairness
    mos = {aid: a.get("mos") for aid, a in apps.items() if a["app_type"] not in (App.BULK, App.UPLOAD)}
    # bulk apps: MOS from throughput relative to an equal split of what the others leave
    if bulk:
        avg_cap = sum(sn["capacity_down"] for sn in series) / max(1, len(series)) / 1e6
        others = sum(v.get("video_kbps_down", 0) / 1000 for v in c) + sum((v.get("avg_bitrate_mbps") or 0) for v in s)
        fair = max(0.5, (avg_cap - others) / max(1, len([b for b in bulk if not b.get("upload")])))
        avg_up = sum(sn["capacity_up"] for sn in series) / max(1, len(series)) / 1e6
        fair_up = max(0.2, (avg_up - sum(v.get("video_kbps_up", 0) / 1000 for v in c)) /
                      max(1, len([b for b in bulk if b.get("upload")])))
        for b in bulk:
            mos[b["app_id"]] = M.bulk_mos(b["avg_throughput_mbps"], fair_up if b.get("upload") else fair)
    vals = [v for v in mos.values() if v is not None]
    inter = [a.get("mos") for a in apps.values()
             if a["app_type"] in (App.GAMING, App.VIDEO_CALL, App.STREAMING, App.WEB) and a.get("mos") is not None]
    # what people actively perceive (game, call, video, browsing); bulk transfers are reported
    # separately as throughput because their "experience" is completion time, not interactivity
    out["interactive_mos"] = sum(inter) / len(inter) if inter else None
    out["min_interactive_mos"] = min(inter) if inter else None
    out["mos_by_app"] = mos
    out["qoe_fairness"] = M.qoe_fairness(vals)
    out["jain_qoe"] = M.jain([(v - 1) / 4 for v in vals])
    out["mean_mos"] = sum(vals) / len(vals) if vals else None
    out["min_mos"] = min(vals) if vals else None
    dl = [b for b in bulk if not b.get("upload")]
    if len(dl) >= 2:
        out["jain_bulk_devices"] = M.jain([b["avg_throughput_mbps"] for b in dl])
    return out


def run_sim(scenario: str, scheduler: str, seed: int = 1, duration: Optional[float] = None,
            intent: str = "balanced", classifier=None, on_second: Optional[Callable[[dict], None]] = None,
            keep_series: bool = True) -> dict:
    sc = get(scenario, seed)
    qos = make_qos(scheduler, intent=intent, classifier=classifier)
    w = HomeWorld(sc.link, qos, seed=seed)
    build_sim(w, sc)
    series: list[dict] = []
    nxt = [1.0]

    def on_step(t: float) -> None:
        if t + 1e-9 >= nxt[0]:
            nxt[0] += 1.0
            snap = collect(w, t, 1.0)
            series.append(snap)
            if on_second is not None:
                on_second(snap)

    t0 = time.time()
    w.run(duration or sc.duration, step=0.1, on_step=on_step)
    wall = time.time() - t0
    res = {
        "scenario": scenario, "scheduler": scheduler, "label": SCHEDULER_LABELS.get(scheduler, scheduler),
        "seed": seed, "duration": duration or sc.duration, "wall_s": wall, "events": w.sim.events,
        "summary": summarize(w, series), "timeline": w.event_log,
        "decisions": list(getattr(qos, "decisions", []))[-300:],
    }
    if keep_series:
        res["series"] = series
    if hasattr(qos, "brain") and qos.brain is not None:
        res["classifier_stats"] = dict(qos.brain.stats)
        res["flow_accuracy"] = flow_accuracy(w, qos.brain)
    return res


def flow_accuracy(w: HomeWorld, brain) -> dict:
    """Ground-truth check of the controller's labels (byte-weighted and per flow)."""
    tot = ok = 0
    wb = wok = 0
    conf: dict = {}
    by_key = w.flow_by_key
    for key, label, b in brain.labelled_flows():
        f = by_key.get(key)
        if f is None:
            continue
        tot += 1
        wb += b
        conf.setdefault(f.app_type, {}).setdefault(label, 0)
        conf[f.app_type][label] += 1
        if label == f.app_type:
            ok += 1
            wok += b
    return {"flows": tot, "accuracy": ok / tot if tot else None, "byte_accuracy": wok / wb if wb else None,
            "confusion": conf}
