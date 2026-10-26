"""Live sessions behind the dashboard: simulator (any OS) or real Linux testbed (root).

The manager owns at most one live session, fans every 1 s telemetry tick out to all
WebSocket clients, and persists it (samples, decisions, flows, learned device profiles).
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import time
from typing import Any, Optional

from fastapi import WebSocket

from ..bench.runner import SCHEDULER_LABELS
from ..bench.scenarios import get
from ..common.classes import TC
from ..db.store import Store, _clean
from ..sim.world import DEFAULT_DEVICES
from . import sim_worker
from .presets import PRESETS, SANDBOX_APPS


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.last: Optional[dict] = None

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        if self.last is not None:
            await ws.send_json(self.last)

    def leave(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, msg: dict, remember: bool = True) -> None:
        if remember:
            self.last = msg
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


class LiveSession:
    mode = "base"

    def __init__(self, mgr: "Manager", params: dict) -> None:
        self.mgr = mgr
        self.params = params
        self.started = time.time()
        self.run_ids: dict[str, int] = {}
        self.status = "starting"
        self.ticks = 0
        self.error: Optional[str] = None

    def info(self) -> dict:
        return {"mode": self.mode, "status": self.status, "params": self.params, "run_ids": self.run_ids,
                "started": self.started, "ticks": self.ticks, "error": self.error}

    async def on_tick(self, tick: dict) -> None:
        tick = _clean(tick)
        self.ticks += 1
        tick["mode"] = self.mode
        tick["session"] = {"status": self.status, "params": self.params, "run_ids": self.run_ids}
        await self.mgr.hub.broadcast(tick)
        st = self.mgr.store
        for side, snap in tick.get("sides", {}).items():
            rid = self.run_ids.get(side)
            if rid is None:
                continue
            compact = {k: snap.get(k) for k in ("scheduler", "capacity", "shaper", "isp_queue_ms", "apps", "classes")}
            st.add_sample(rid, tick["t"], compact, side)
            for d in snap.get("decisions", []):
                st.add_decision(rid, d)
            if self.ticks % 5 == 0 and snap.get("flows"):
                rows = snap["flows"]
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, st.upsert_flows, rid, rows)
                devs = {d.name: {"ip": d.ip, "kind": d.kind} for d in DEFAULT_DEVICES}
                loop.run_in_executor(None, st.learn_profiles, devs, rows)


class SimLive(LiveSession):
    mode = "sim"

    async def start(self) -> None:
        p = self.params
        scheds = {"a": p["scheduler"]}
        if p.get("compare_with"):
            scheds = {"a": p["compare_with"], "b": p["scheduler"]}
        cfg = {"schedulers": scheds, "scenario": p.get("scenario", "sandbox"), "seed": p.get("seed", 1),
               "speed": p.get("speed", 1.0), "intent": p.get("intent", "balanced")}
        for side, sch in scheds.items():
            self.run_ids[side] = self.mgr.store.create_run(
                kind="live", mode="sim", scenario=cfg["scenario"], scheduler=sch, seed=cfg["seed"],
                intent=cfg["intent"], label=f"live {SCHEDULER_LABELS.get(sch, sch)} ({side})", meta={"side": side})
        ctx = mp.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=sim_worker.main, args=(child, cfg), daemon=True)
        self.proc.start()
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(self.conn.fileno(), self._readable)
        self.status = "running"

    def _readable(self) -> None:
        try:
            while self.conn.poll():
                msg = self.conn.recv()
                t = msg.get("type")
                if t == "tick":
                    self.loop.create_task(self.on_tick(msg))
                elif t == "error":
                    self.error = msg.get("error")
                    self.status = "error"
                    print(msg.get("trace"))
                elif t == "finished":
                    self.status = "finished"
                elif t == "ack" and not msg.get("ok"):
                    self.error = msg.get("error")
        except (EOFError, OSError):
            self.loop.remove_reader(self.conn.fileno())
            if self.status == "running":
                self.status = "stopped"

    async def action(self, msg: dict) -> dict:
        typ = msg.get("type")
        if typ == "preset":
            if msg["name"] not in PRESETS:
                raise KeyError(msg["name"])
            self.conn.send({"cmd": "preset", "name": msg["name"]})
        elif typ == "stop_app":
            self.conn.send({"cmd": "stop_app", "app_id": msg["app_id"]})
        elif typ == "capacity":
            self.conn.send({"cmd": "capacity", "down": msg.get("down"), "up": msg.get("up")})
        elif typ == "intent":
            self.conn.send({"cmd": "intent", "name": msg["name"]})
            self.params["intent"] = msg["name"]
        elif typ == "scheduler":
            self.conn.send({"cmd": "scheduler", "side": msg.get("side", "a"), "name": msg["name"]})
        elif typ == "pin":
            self.conn.send({"cmd": "pin", "device": msg["device"], "tc": msg.get("tc")})
        elif typ == "speed":
            self.conn.send({"cmd": "speed", "value": msg["value"]})
        else:
            raise ValueError(f"unknown action {typ}")
        return {"ok": True}

    async def stop(self) -> None:
        try:
            self.conn.send({"cmd": "stop"})
        except OSError:
            pass
        try:
            self.loop.remove_reader(self.conn.fileno())
        except (ValueError, OSError):
            pass
        await asyncio.get_running_loop().run_in_executor(None, self.proc.join, 3)
        if self.proc.is_alive():
            self.proc.terminate()
        for rid in self.run_ids.values():
            self.mgr.store.finish_run(rid, status="done", duration_s=time.time() - self.started)
        self.status = "stopped"


class KernelLive(LiveSession):
    """The real Linux testbed driven live from the dashboard."""

    mode = "kernel"

    async def start(self) -> None:
        from ..bench.kernel_runner import KernelSession
        from ..sim.world import LinkProfile

        p = self.params
        scen = p.get("scenario", "sandbox")
        link = LinkProfile() if scen == "sandbox" else get(scen, p.get("seed", 1)).link
        self.sched = p["scheduler"]
        self.sess = KernelSession(self.sched, link, p.get("seed", 1), p.get("intent", "balanced"))
        await self.sess.start()
        self.run_ids["a"] = self.mgr.store.create_run(
            kind="live", mode="kernel", scenario=scen, scheduler=self.sched, seed=p.get("seed", 1),
            intent=p.get("intent", "balanced"), label=f"live kernel {SCHEDULER_LABELS.get(self.sched, self.sched)}")
        self.events: list = []
        apps = SANDBOX_APPS if scen == "sandbox" else get(scen, p.get("seed", 1)).apps
        self.pending = sorted(((a.start, a) for a in apps), key=lambda x: x[0])
        self.caps = [] if scen == "sandbox" else list(get(scen, p.get("seed", 1)).caps)
        self.duration = None if scen == "sandbox" else get(scen, p.get("seed", 1)).duration
        self.sent_dec = 0
        self.status = "running"
        self.task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self) -> None:
        next_tick = 1.0
        try:
            while self.status == "running":
                t = time.monotonic() - self.sess.t0
                while self.pending and self.pending[0][0] <= t:
                    _, a = self.pending.pop(0)
                    self.sess.start_app(a.app_type, a.app_id, a.device, a.server, a.params)
                while self.caps and self.caps[0].t <= t:
                    ev = self.caps.pop(0)
                    self.sess.set_capacity(ev.down_mbps, ev.up_mbps)
                if t >= next_tick:
                    next_tick += 1.0
                    await self.on_tick({"type": "tick", "t": round(t, 2), "speed": 1.0,
                                        "sides": {"a": self._snapshot()}, "events": self.events[-5:]})
                if self.duration is not None and t >= self.duration:
                    self.status = "finished"
                await asyncio.sleep(0.05)
        except Exception as exc:  # pragma: no cover
            self.error = repr(exc)
            self.status = "error"

    def _snapshot(self) -> dict:
        s = self.sess.snapshot()
        out = {"scheduler": self.sched, "label": SCHEDULER_LABELS.get(self.sched, self.sched),
               "capacity": {"down": s["capacity_down"], "up": s["capacity_up"]},
               "shaper": {"down": s.get("shaper_down"), "up": s.get("shaper_up")},
               "isp_queue_ms": {"down": s["isp_queue_ms_down"], "up": s["isp_queue_ms_up"]},
               "apps": s["apps"]}
        ctrl = self.sess.ctrl
        if ctrl is not None:
            decs = ctrl.decisions
            out["decisions"] = decs[self.sent_dec:][-50:]
            self.sent_dec = len(decs)
            if ctrl.brain is not None:
                out["classes"] = ctrl.brain.class_status
                out["flows"] = ctrl.brain.flow_table(time.monotonic(), 60)
                out["classifier"] = {"kind": ctrl.brain.clf.kind, **self.sess.flow_accuracy(), **ctrl.brain.stats}
                out["intent"] = ctrl.brain.intent
        else:
            out["decisions"] = []
        if out["shaper"]["down"] is None and self.sched != "fifo":
            out["shaper"] = {"down": self.sess.topo.down_mbps * 0.95e6, "up": self.sess.topo.up_mbps * 0.95e6}
        return out

    async def action(self, msg: dict) -> dict:
        typ = msg.get("type")
        t = round(time.monotonic() - self.sess.t0, 2)
        if typ == "preset":
            p = PRESETS[msg["name"]]
            for a in p.get("apps", []):
                self.sess.start_app(a.app_type, a.app_id, a.device, a.server, a.params)
            if "capacity" in p:
                self.sess.set_capacity(*p["capacity"])
            self.events.append({"t": t, "event": p["label"]})
        elif typ == "stop_app":
            self.sess.stop_app(msg["app_id"])
            self.events.append({"t": t, "event": f"stop {msg['app_id']}"})
        elif typ == "capacity":
            self.sess.set_capacity(msg.get("down") or self.sess.topo.cur_down, msg.get("up") or self.sess.topo.cur_up)
        elif typ == "intent":
            if self.sess.ctrl and self.sess.ctrl.brain:
                self.sess.ctrl.brain.set_intent(msg["name"], t)
                self.sess.ctrl.decisions.append(self.sess.ctrl.brain.decisions[-1])
        elif typ == "pin":
            if self.sess.ctrl and self.sess.ctrl.brain:
                self.sess.ctrl.brain.pin_device(msg["device"], TC(msg["tc"]) if msg.get("tc") else None, t)
        elif typ == "scheduler":
            await self._switch(msg["name"])
        else:
            raise ValueError(f"unknown action {typ}")
        return {"ok": True}

    async def _switch(self, name: str) -> None:
        from ..controller.classifier import FlowClassifier
        from ..testbed.kernel_qos import KernelController

        loop = asyncio.get_running_loop()
        if self.sess.ctrl is not None:
            await self.sess.ctrl.stop()
            self.sess.ctrl = None
        topo = self.sess.topo
        await loop.run_in_executor(None, self.sess.dp.setup, name, topo.down_mbps * 0.95e6, topo.up_mbps * 0.95e6)
        if name.startswith("flowpilot") or name == "cake_autorate":
            self.sess.ctrl = KernelController(topo, self.sess.dp, name, FlowClassifier(),
                                              intent=self.params.get("intent", "balanced"))
            await self.sess.ctrl.start()
            self.sess.ctrl.t0 = self.sess.t0
        self.sched = name
        self.sent_dec = 0
        self.events.append({"t": round(time.monotonic() - self.sess.t0, 2), "event": f"router -> {name}"})

    async def stop(self) -> None:
        self.status = "stopping"
        if hasattr(self, "task"):
            self.task.cancel()
        try:
            apps = await self.sess.summaries()
        except Exception:
            apps = None
        await self.sess.stop()
        for rid in self.run_ids.values():
            self.mgr.store.finish_run(rid, summary={"apps": apps} if apps else None, status="done",
                                      duration_s=time.time() - self.started)
        self.status = "stopped"


class Manager:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.hub = Hub()
        self.current: Optional[LiveSession] = None
        self.lock = asyncio.Lock()

    async def start(self, params: dict) -> dict:
        async with self.lock:
            if self.current is not None:
                await self.current.stop()
                self.current = None
            sess: LiveSession = KernelLive(self, params) if params.get("mode") == "kernel" else SimLive(self, params)
            await sess.start()
            self.current = sess
            return sess.info()

    async def stop(self) -> dict:
        async with self.lock:
            if self.current is None:
                return {"status": "idle"}
            await self.current.stop()
            info = self.current.info()
            self.current = None
            await self.hub.broadcast({"type": "stopped"}, remember=False)
            self.hub.last = None
            return info

    async def action(self, msg: dict) -> Any:
        if self.current is None:
            raise RuntimeError("no live session")
        return await self.current.action(msg)

    def info(self) -> dict:
        return self.current.info() if self.current else {"status": "idle"}
