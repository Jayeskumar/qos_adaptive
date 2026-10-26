"""Real-time simulator worker for live dashboard sessions (runs in its own process).

One or two simulated households ("sides") advance in lock-step at wall-clock speed. With
two sides the dashboard shows an A/B comparison: identical households, identical events,
different router (e.g. a standard router on the left, FlowPilot on the right).
"""
from __future__ import annotations

import os
import time
import traceback

os.environ.setdefault("OMP_NUM_THREADS", "1")

from ..bench.runner import SCHEDULER_LABELS, collect, flow_accuracy, make_qos  # noqa: E402
from ..bench.scenarios import build_sim, get  # noqa: E402
from ..common.classes import App, TC  # noqa: E402
from ..sim.apps import APP_CLASSES  # noqa: E402
from ..sim.world import HomeWorld, LinkProfile  # noqa: E402
from .presets import PRESETS, SANDBOX_APPS  # noqa: E402


def _add(w: HomeWorld, spec, start: float) -> None:
    cls = APP_CLASSES[spec.app_type]
    params = dict(spec.params)
    if spec.app_type == App.UPLOAD:
        params["upload"] = True
    old = w.apps.get(spec.app_id)
    if old is not None and old.active:
        return
    w.add_app(cls, spec.app_id, spec.device, spec.server, start=start, **params)


class SimWorker:
    def __init__(self, conn, cfg: dict) -> None:
        self.conn = conn
        self.cfg = cfg
        self.speed = float(cfg.get("speed", 1.0))
        self.intent = cfg.get("intent", "balanced")
        self.scenario = cfg.get("scenario", "sandbox")
        seed = int(cfg.get("seed", 1))
        if self.scenario == "sandbox":
            link = LinkProfile(**cfg.get("link", {}))
            self.duration = None
        else:
            sc = get(self.scenario, seed)
            link = sc.link
            self.duration = sc.duration
        self.sides: dict[str, HomeWorld] = {}
        self.sent_dec: dict[str, int] = {}
        for side, sched in cfg["schedulers"].items():
            w = HomeWorld(link, make_qos(sched, intent=self.intent), seed=seed)
            if self.scenario == "sandbox":
                for spec in SANDBOX_APPS:
                    _add(w, spec, spec.start)
            else:
                build_sim(w, get(self.scenario, seed))
            self.sides[side] = w
            self.sent_dec[side] = 0
        self.stop = False
        self.t = 0.0

    # ------------------------------------------------------------------ commands
    def _poll(self) -> None:
        while self.conn.poll():
            msg = self.conn.recv()
            try:
                self._handle(msg)
                self.conn.send({"type": "ack", "cmd": msg.get("cmd"), "ok": True})
            except Exception as exc:
                self.conn.send({"type": "ack", "cmd": msg.get("cmd"), "ok": False, "error": repr(exc)})

    def _handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        now = self.t
        if cmd == "stop":
            self.stop = True
        elif cmd == "preset":
            p = PRESETS[msg["name"]]
            for w in self.sides.values():
                for spec in p.get("apps", []):
                    _add(w, spec, now + 0.05)
                if "capacity" in p:
                    d, u = p["capacity"]
                    w.at(now + 0.05, lambda w=w, d=d, u=u: w.set_capacity(d, u), p["label"])
        elif cmd == "stop_app":
            for w in self.sides.values():
                a = w.apps.get(msg["app_id"])
                if a is not None and a.active:
                    w.at(now + 0.05, a.stop, f"stop {msg['app_id']}")
        elif cmd == "capacity":
            for w in self.sides.values():
                w.at(now + 0.05, lambda w=w: w.set_capacity(msg.get("down"), msg.get("up")),
                     f"capacity {msg.get('down')}/{msg.get('up')} Mb/s")
        elif cmd == "intent":
            self.intent = msg["name"]
            for w in self.sides.values():
                brain = getattr(w.qos, "brain", None)
                if brain is not None:
                    brain.set_intent(self.intent, now)
                    w.qos.decisions.append(brain.decisions[-1])
        elif cmd == "scheduler":
            w = self.sides[msg.get("side", "a")]
            w.set_qos(make_qos(msg["name"], intent=self.intent))
            self.sent_dec[msg.get("side", "a")] = 0
        elif cmd == "pin":
            for w in self.sides.values():
                brain = getattr(w.qos, "brain", None)
                if brain is not None:
                    brain.pin_device(msg["device"], TC(msg["tc"]) if msg.get("tc") else None, now)
        elif cmd == "speed":
            self.speed = max(0.25, min(8.0, float(msg["value"])))

    # ------------------------------------------------------------------ telemetry
    def _snapshot(self, side: str, w: HomeWorld) -> dict:
        snap = collect(w, self.t, 1.0)
        qos = w.qos
        out = {
            "scheduler": qos.name, "label": SCHEDULER_LABELS.get(qos.name, getattr(qos, "label", qos.name)),
            "capacity": {"down": snap["capacity_down"], "up": snap["capacity_up"]},
            "shaper": {"down": snap.get("shaper_down"), "up": snap.get("shaper_up")},
            "isp_queue_ms": {"down": snap["isp_queue_ms_down"], "up": snap["isp_queue_ms_up"]},
            "apps": snap["apps"],
        }
        if out["shaper"]["down"] is None and qos.name in ("fq_codel", "cake", "static_qos"):
            out["shaper"] = {"down": w.link.down_mbps * 1e6 * 0.95, "up": w.link.up_mbps * 1e6 * 0.95}
        brain = getattr(qos, "brain", None)
        decs = getattr(qos, "decisions", [])
        new = decs[self.sent_dec[side]:]
        self.sent_dec[side] = len(decs)
        out["decisions"] = new[-50:]
        if brain is not None:
            out["classes"] = brain.class_status
            out["flows"] = brain.flow_table(self.t, 60)
            out["classifier"] = {"kind": brain.clf.kind, **flow_accuracy(w, brain), **brain.stats}
            out["intent"] = brain.intent
        return out

    def run(self) -> None:
        wall0 = time.monotonic()
        next_tick = 1.0
        lag_reported = 0.0
        self.conn.send({"type": "ready", "sides": {k: w.qos.name for k, w in self.sides.items()}})
        while not self.stop:
            self._poll()
            if self.stop:
                break
            self.t = round(self.t + 0.1, 3)
            for w in self.sides.values():
                w.run(self.t, step=0.1)
            if self.t + 1e-9 >= next_tick:
                next_tick += 1.0
                tick = {"type": "tick", "t": self.t, "speed": self.speed,
                        "sides": {k: self._snapshot(k, w) for k, w in self.sides.items()},
                        "events": [e for w in list(self.sides.values())[:1] for e in w.event_log[-5:]]}
                self.conn.send(tick)
            if self.duration is not None and self.t >= self.duration:
                self.conn.send({"type": "finished", "t": self.t})
                break
            target = wall0 + self.t / self.speed
            d = target - time.monotonic()
            if d > 0:
                time.sleep(d)
            elif d < -1.0:
                wall0 -= d  # cannot keep up: run as fast as possible without trying to catch up
                lag_reported += -d


def main(conn, cfg: dict) -> None:
    try:
        SimWorker(conn, cfg).run()
    except Exception as exc:  # pragma: no cover - reported to the API
        conn.send({"type": "error", "error": repr(exc), "trace": traceback.format_exc()})
