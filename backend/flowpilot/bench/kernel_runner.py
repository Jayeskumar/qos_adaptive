"""Run a scenario on the real-kernel Linux testbed (requires root and network namespaces).

    python -m flowpilot.bench.kernel_runner --scenario evening_rush --scheduler flowpilot

Same scenario definitions and the same metric computations as the simulator; the network,
the TCP/UDP stacks, the qdiscs and the measured delays are all real Linux.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import time
from pathlib import Path
from typing import Callable, Optional

from ..controller.classifier import FlowClassifier
from ..testbed import runner as traffic_runner
from ..testbed.dataplane import KernelDataplane
from ..testbed.kernel_qos import KernelController
from ..testbed.netns import sh
from ..testbed.topology import NS_ISP, Topology
from .runner import SCHEDULER_LABELS, summarize_apps
from .scenarios import get


class KernelSession:
    """A live testbed session: topology + data plane + traffic runner (+ controller)."""

    def __init__(self, scheduler: str, link, seed: int = 1, intent: str = "balanced",
                 classifier: Optional[FlowClassifier] = None) -> None:
        self.scheduler = scheduler
        self.link = link
        self.seed = seed
        self.intent = intent
        self.clf = classifier
        self.apps_msg: Optional[dict] = None
        self.truth: dict = {}
        self.errors: list = []
        self._summary_fut: Optional[asyncio.Future] = None
        self.t0 = 0.0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.topo = Topology(down_mbps=self.link.down_mbps, up_mbps=self.link.up_mbps,
                             isp_buffer_ms_down=self.link.isp_buffer_ms_down,
                             isp_buffer_ms_up=self.link.isp_buffer_ms_up)
        await loop.run_in_executor(None, self.topo.build)
        await loop.run_in_executor(None, self.topo.start_delayline)
        self.dp = KernelDataplane()
        rf = 0.95
        await loop.run_in_executor(None, self.dp.setup, self.scheduler, self.link.down_mbps * 1e6 * rf,
                                   self.link.up_mbps * 1e6 * rf)
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        cfg = {"devices": {d.name: d.ip for d in self.topo.devices},
               "servers": {s.name: s.ip for s in self.topo.servers},
               "extra_owd": {s.name: self.topo.extra_owd_ms(s.name) for s in self.topo.servers},
               "seed": self.seed}
        self.proc = ctx.Process(target=traffic_runner.main, args=(child, cfg), daemon=True)
        self.proc.start()
        self._ready = loop.create_future()
        loop.add_reader(self.conn.fileno(), self._readable)
        await asyncio.wait_for(self._ready, 20)
        self.ctrl: Optional[KernelController] = None
        if self.scheduler.startswith("flowpilot") or self.scheduler == "cake_autorate":
            self.ctrl = KernelController(self.topo, self.dp, self.scheduler, self.clf or FlowClassifier(),
                                         intent=self.intent)
            await self.ctrl.start()
        self.t0 = time.monotonic()
        if self.ctrl is not None:
            self.ctrl.t0 = self.t0

    def _readable(self) -> None:
        try:
            while self.conn.poll():
                msg = self.conn.recv()
                t = msg.get("type")
                if t == "ready" and not self._ready.done():
                    self._ready.set_result(True)
                elif t == "apps":
                    self.apps_msg = msg
                    for key, app_type, app_id in msg.get("truth", []):
                        self.truth[tuple(key)] = (app_type, app_id)
                elif t == "summary" and self._summary_fut is not None and not self._summary_fut.done():
                    self._summary_fut.set_result(msg["apps"])
                elif t == "error":
                    self.errors.append(msg)
        except (EOFError, OSError):
            asyncio.get_running_loop().remove_reader(self.conn.fileno())

    # ------------------------------------------------------------------ controls
    def start_app(self, app_type: str, app_id: str, device: str, server: str, params: Optional[dict] = None) -> None:
        self.conn.send({"cmd": "start_app", "app_type": app_type, "app_id": app_id, "device": device,
                        "server": server, "params": params or {}})

    def stop_app(self, app_id: str) -> None:
        self.conn.send({"cmd": "stop_app", "app_id": app_id})

    def set_capacity(self, down: float, up: float) -> None:
        self.topo.set_capacity(down, up)

    def isp_queue_ms(self) -> tuple[float, float]:
        out = []
        for dev, cap in (("x-home", self.topo.cur_down), ("x-inet", self.topo.cur_up)):
            try:
                q = json.loads(sh(f"tc -n {NS_ISP} -s -j qdisc show dev {dev}"))
                b = next((x.get("backlog", 0) for x in q if x.get("kind") == "tbf"), 0)
                out.append(b * 8 / (cap * 1e6) * 1000)
            except Exception:
                out.append(0.0)
        return out[0], out[1]

    def snapshot(self) -> dict:
        t = time.monotonic() - self.t0
        qd, qu = self.isp_queue_ms()
        snap = {"t": round(t, 2), "capacity_down": self.topo.cur_down * 1e6, "capacity_up": self.topo.cur_up * 1e6,
                "isp_queue_ms_down": qd, "isp_queue_ms_up": qu,
                "apps": [a for a in (self.apps_msg or {}).get("apps", []) if "error" not in a]}
        if self.ctrl is not None:
            st = self.ctrl.status()
            snap["shaper_down"] = st["autorate"]["down"]["rate"]
            snap["shaper_up"] = st["autorate"]["up"]["rate"]
            if "classes" in st:
                snap["classes"] = st["classes"]
        return snap

    async def summaries(self) -> dict:
        self._summary_fut = asyncio.get_running_loop().create_future()
        self.conn.send({"cmd": "summary"})
        return await asyncio.wait_for(self._summary_fut, 10)

    def flow_accuracy(self) -> dict:
        if self.ctrl is None or self.ctrl.brain is None:
            return {}
        tot = ok = 0
        wb = wok = 0
        conf: dict = {}
        for key, fs in self.ctrl.brain.flows.items():
            tr = self.truth.get(key)
            if tr is None or fs.label is None:
                continue
            tot += 1
            b = fs.b_up + fs.b_down
            wb += b
            conf.setdefault(tr[0], {}).setdefault(fs.label, 0)
            conf[tr[0]][fs.label] += 1
            if fs.label == tr[0]:
                ok += 1
                wok += b
        return {"flows": tot, "accuracy": ok / tot if tot else None, "byte_accuracy": wok / wb if wb else None,
                "confusion": conf}

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        if self.ctrl is not None:
            await self.ctrl.stop()
        try:
            self.conn.send({"cmd": "shutdown"})
        except OSError:
            pass
        loop.remove_reader(self.conn.fileno())
        await loop.run_in_executor(None, self.proc.join, 5)
        if self.proc.is_alive():
            self.proc.terminate()
        self.topo.stop_delayline()
        await loop.run_in_executor(None, Topology.teardown)


async def run_kernel_async(scenario: str, scheduler: str, seed: int = 1, duration: Optional[float] = None,
                           intent: str = "balanced", classifier=None,
                           on_second: Optional[Callable[[dict], None]] = None) -> dict:
    sc = get(scenario, seed)
    dur = duration or sc.duration
    sess = KernelSession(scheduler, sc.link, seed, intent, classifier)
    await sess.start()
    events = []
    for a in sc.apps:
        events.append((a.start, "start", a))
        if a.stop is not None:
            events.append((a.stop, "stop", a))
    for ev in sc.caps:
        events.append((ev.t, "cap", ev))
    events.sort(key=lambda e: e[0])
    timeline = []
    series = []
    try:
        i = 0
        next_snap = 1.0
        while True:
            t = time.monotonic() - sess.t0
            while i < len(events) and events[i][0] <= t:
                et, kind, obj = events[i]
                if kind == "start":
                    sess.start_app(obj.app_type, obj.app_id, obj.device, obj.server, obj.params)
                    timeline.append({"t": round(t, 2), "event": f"start {obj.app_id}"})
                elif kind == "stop":
                    sess.stop_app(obj.app_id)
                    timeline.append({"t": round(t, 2), "event": f"stop {obj.app_id}"})
                else:
                    sess.set_capacity(obj.down_mbps, obj.up_mbps)
                    if obj.label:
                        timeline.append({"t": round(t, 2), "event": obj.label})
                i += 1
            if t >= next_snap:
                next_snap += 1.0
                snap = sess.snapshot()
                series.append(snap)
                if on_second is not None:
                    on_second(snap)
            if t >= dur:
                break
            await asyncio.sleep(0.05)
        apps = await sess.summaries()
        res = {
            "mode": "kernel", "scenario": scenario, "scheduler": scheduler,
            "label": SCHEDULER_LABELS.get(scheduler, scheduler), "seed": seed, "duration": dur,
            "summary": summarize_apps(apps, series), "series": series, "timeline": timeline,
            "decisions": list(sess.ctrl.decisions[-300:]) if sess.ctrl else [],
            "flow_accuracy": sess.flow_accuracy(), "errors": sess.errors,
            "dataplane": {"leaf": sess.dp.leaf, "errors": sess.dp.errors},
        }
        if sess.ctrl is not None:
            res["controller"] = {k: v for k, v in sess.ctrl.status().items() if k != "classes"}
            if sess.ctrl.brain is not None:
                res["classifier_stats"] = dict(sess.ctrl.brain.stats)
        return res
    finally:
        await sess.stop()


def run_kernel(*args, **kw) -> dict:
    return asyncio.run(run_kernel_async(*args, **kw))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="evening_rush")
    ap.add_argument("--scheduler", default="flowpilot")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    r = run_kernel(a.scenario, a.scheduler, a.seed, a.duration)
    s = r["summary"]
    keys = ["game_rtt_p50_ms", "game_rtt_p99_ms", "game_loss_pct", "call_owd_p95_ms", "call_owd_p99_ms",
            "call_loss_pct", "call_mos", "stream_bitrate_mbps", "stream_rebuffer_pct", "web_plt_p50_s",
            "bulk_mbps", "upload_mbps", "utilization_down_pct", "qoe_fairness", "mean_mos"]
    print(json.dumps({k: (round(s[k], 2) if isinstance(s.get(k), float) else s.get(k)) for k in keys}, indent=1))
    print("flow accuracy:", r.get("flow_accuracy"), "errors:", r.get("errors")[:3])
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(r))


if __name__ == "__main__":
    main()
