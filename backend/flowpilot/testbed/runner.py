"""Traffic-runner process: hosts every live application and content server in one loop.

Commands arrive over a multiprocessing pipe ({"cmd": "start_app", ...}); once per second
the runner sends per-application snapshots plus the ground-truth 5-tuples of new flows
(used only to score the classifier, never to steer it).
"""
from __future__ import annotations

import asyncio
import random
import time

from .apps import LIVE_APPS, ContentServer, Env


class TrafficRunner:
    def __init__(self, conn, cfg: dict) -> None:
        self.conn = conn
        self.cfg = cfg
        self.apps: dict = {}
        self.truth: list = []
        self.rng = random.Random(cfg.get("seed", 1))
        self.done = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        devs = self.cfg["devices"]
        srvs = self.cfg["servers"]
        extra = self.cfg.get("extra_owd", {})
        self.env = Env(dev_ns=lambda d: f"fp-dev-{d}", dev_ip=lambda d: devs[d], srv_ip=lambda s: srvs[s],
                       extra_owd_ms=lambda s: extra.get(s, 0.0), on_flow=self._on_flow)
        self.servers = [ContentServer("fp-inet", srvs[n]) for n in ("cdn", "mirror", "web", "backup")]
        self.server_tasks = [loop.create_task(s.serve()) for s in self.servers]
        loop.add_reader(self.conn.fileno(), self._readable)
        reporter = loop.create_task(self._report())
        self.conn.send({"type": "ready"})
        await self.done.wait()
        reporter.cancel()
        for app in list(self.apps.values()):
            if app.active:
                await app.stop()
        for t in self.server_tasks:
            t.cancel()

    def _on_flow(self, key: tuple, app_type: str, app_id: str) -> None:
        self.truth.append((list(key), app_type, app_id))

    def _readable(self) -> None:
        try:
            while self.conn.poll():
                self._dispatch(self.conn.recv())
        except (EOFError, OSError):
            self.done.set()

    def _dispatch(self, msg: dict) -> None:
        loop = asyncio.get_running_loop()
        cmd = msg.get("cmd")
        if cmd == "start_app":
            cls = LIVE_APPS[msg["app_type"]]
            params = dict(msg.get("params", {}))
            if msg["app_type"] == "upload":
                params["upload"] = True
            app = cls(self.env, msg["app_id"], msg["device"], msg["server"],
                      random.Random(self.rng.random()), **params)
            self.apps[msg["app_id"]] = app
            loop.create_task(self._safe(app.start(), msg["app_id"]))
        elif cmd == "stop_app":
            app = self.apps.get(msg["app_id"])
            if app is not None and app.active:
                loop.create_task(app.stop())
        elif cmd == "summary":
            self.conn.send({"type": "summary", "apps": {k: self._safe_summary(a) for k, a in self.apps.items()}})
        elif cmd == "shutdown":
            self.done.set()

    async def _safe(self, coro, app_id: str) -> None:
        try:
            await coro
        except Exception as exc:  # surface failures to the controller instead of dying silently
            self.conn.send({"type": "error", "app_id": app_id, "error": repr(exc)})

    @staticmethod
    def _safe_summary(app) -> dict:
        try:
            return app.summary()
        except Exception as exc:
            return {"app_id": app.app_id, "app_type": app.app_type, "error": repr(exc)}

    async def _report(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(1.0 - (time.monotonic() - last) % 1.0)
            now = time.monotonic()
            dt = max(1e-3, now - last)
            last = now
            snaps = []
            for a in self.apps.values():
                if a.active or (a.t_stop and now - a.t_stop < dt):
                    try:
                        snaps.append(a.snapshot(dt))
                    except Exception as exc:  # pragma: no cover
                        snaps.append({"app_id": a.app_id, "app_type": a.app_type, "error": repr(exc), "active": a.active})
            truth, self.truth = self.truth, []
            try:
                self.conn.send({"type": "apps", "t": now, "apps": snaps, "truth": truth})
            except (BrokenPipeError, OSError):
                self.done.set()
                return


def main(conn, cfg: dict) -> None:
    try:
        import os

        os.nice(-2)
    except OSError:
        pass
    asyncio.run(TrafficRunner(conn, cfg).run())
