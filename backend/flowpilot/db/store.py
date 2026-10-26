"""Database engine + a background writer so the control loop never waits on I/O.

DATABASE_URL selects the backend, e.g.
    postgresql+psycopg://flowpilot:flowpilot@localhost:5432/flowpilot
and defaults to a local SQLite file so `python -m flowpilot.api` works with no setup.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import create_engine, delete, desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Decision, DeviceProfile, FlowRecord, Run, Sample

DEFAULT_URL = "sqlite:///" + os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "flowpilot.db")


def _clean(o: Any) -> Any:
    """Make telemetry JSON-safe (NaN/inf -> None, tuples -> lists)."""
    if isinstance(o, float):
        return None if o != o or o in (float("inf"), float("-inf")) else round(o, 4)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


class Store:
    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
        kw = {"pool_pre_ping": True}
        if self.url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(self.url, **kw)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.q: queue.Queue = queue.Queue(maxsize=20000)
        self._t = threading.Thread(target=self._writer, name="db-writer", daemon=True)
        self._t.start()

    @property
    def backend(self) -> str:
        return self.engine.dialect.name

    # ------------------------------------------------------------------ async writes
    def _writer(self) -> None:
        while True:
            batch = [self.q.get()]
            t0 = time.time()
            while len(batch) < 500 and time.time() - t0 < 0.5:
                try:
                    batch.append(self.q.get(timeout=0.1))
                except queue.Empty:
                    break
            try:
                with self.Session() as s:
                    for obj in batch:
                        s.add(obj)
                    s.commit()
            except Exception as exc:  # pragma: no cover - keep the writer alive
                print("db writer error:", exc)

    def put(self, obj) -> None:
        try:
            self.q.put_nowait(obj)
        except queue.Full:
            pass

    # ------------------------------------------------------------------ runs
    def create_run(self, **kw) -> int:
        with self.Session() as s:
            r = Run(**{k: _clean(v) if isinstance(v, (dict, list)) else v for k, v in kw.items()})
            s.add(r)
            s.commit()
            return r.id

    def finish_run(self, run_id: int, summary: Optional[dict] = None, status: str = "done",
                   duration_s: Optional[float] = None) -> None:
        with self.Session() as s:
            r = s.get(Run, run_id)
            if r is None:
                return
            r.status = status
            r.finished_at = datetime.now(timezone.utc)
            if summary is not None:
                r.summary = _clean(summary)
            if duration_s is not None:
                r.duration_s = duration_s
            s.commit()

    def add_sample(self, run_id: int, t: float, data: dict, side: str = "a") -> None:
        self.put(Sample(run_id=run_id, t=t, data=_clean(data), side=side))

    def add_decision(self, run_id: int, d: dict) -> None:
        self.put(Decision(run_id=run_id, t=float(d.get("t", 0.0)), kind=str(d.get("kind", "")),
                          message=str(d.get("message", "")), details=_clean(d.get("details"))))

    def upsert_flows(self, run_id: int, rows: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        with self.Session() as s:
            for r in rows:
                key = json.dumps(r["key"])
                obj = s.scalar(select(FlowRecord).where(FlowRecord.run_id == run_id, FlowRecord.flow_key == key))
                if obj is None:
                    obj = FlowRecord(run_id=run_id, flow_key=key, device=r["device"], proto=r["proto"],
                                     wan_ip=r["wan_ip"], wan_port=int(r["wan_port"]))
                    s.add(obj)
                obj.label = r.get("label")
                obj.confidence = r.get("confidence")
                obj.tc = r.get("tc")
                obj.kbps_up = r.get("kbps_up")
                obj.kbps_down = r.get("kbps_down")
                obj.explanation = r.get("explanation")
                obj.last_seen = now
            s.commit()

    def learn_profiles(self, devices: dict, rows: list[dict]) -> None:
        """Accumulate per-device knowledge: which applications it runs and how much it needs."""
        agg: dict = {}
        for r in rows:
            if not r.get("label"):
                continue
            a = agg.setdefault(r["device"], {})
            e = a.setdefault(r["label"], {"flows": 0, "kbps_down": 0.0, "kbps_up": 0.0})
            e["flows"] += 1
            e["kbps_down"] += r.get("kbps_down") or 0.0
            e["kbps_up"] += r.get("kbps_up") or 0.0
        if not agg:
            return
        with self.Session() as s:
            for dev, apps in agg.items():
                p = s.scalar(select(DeviceProfile).where(DeviceProfile.device == dev))
                if p is None:
                    info = devices.get(dev, {})
                    p = DeviceProfile(device=dev, ip=info.get("ip", ""), kind=info.get("kind", ""), profile={})
                    s.add(p)
                prof = dict(p.profile or {})
                for app, e in apps.items():
                    cur = prof.get(app, {"seen_s": 0, "peak_kbps_down": 0.0, "peak_kbps_up": 0.0,
                                         "avg_kbps_down": 0.0, "avg_kbps_up": 0.0})
                    cur["seen_s"] = cur.get("seen_s", 0) + 1
                    a = 0.05
                    cur["avg_kbps_down"] = round((1 - a) * cur["avg_kbps_down"] + a * e["kbps_down"], 1)
                    cur["avg_kbps_up"] = round((1 - a) * cur["avg_kbps_up"] + a * e["kbps_up"], 1)
                    cur["peak_kbps_down"] = round(max(cur["peak_kbps_down"], e["kbps_down"]), 1)
                    cur["peak_kbps_up"] = round(max(cur["peak_kbps_up"], e["kbps_up"]), 1)
                    prof[app] = cur
                p.profile = prof
            s.commit()

    # ------------------------------------------------------------------ queries
    def list_runs(self, limit: int = 50, kind: Optional[str] = None) -> list[dict]:
        with self.Session() as s:
            q = select(Run).order_by(desc(Run.created_at)).limit(limit)
            if kind:
                q = q.where(Run.kind == kind)
            return [self._run_dict(r) for r in s.scalars(q)]

    def get_run(self, run_id: int) -> Optional[dict]:
        with self.Session() as s:
            r = s.get(Run, run_id)
            return self._run_dict(r, full=True) if r else None

    def run_samples(self, run_id: int, side: Optional[str] = None, limit: int = 3600) -> list[dict]:
        with self.Session() as s:
            q = select(Sample).where(Sample.run_id == run_id).order_by(Sample.t).limit(limit)
            if side:
                q = q.where(Sample.side == side)
            return [{"t": x.t, "side": x.side, **x.data} for x in s.scalars(q)]

    def run_decisions(self, run_id: int, limit: int = 500) -> list[dict]:
        with self.Session() as s:
            q = select(Decision).where(Decision.run_id == run_id).order_by(desc(Decision.t)).limit(limit)
            return [{"t": d.t, "kind": d.kind, "message": d.message, "details": d.details} for d in s.scalars(q)]

    def profiles(self) -> list[dict]:
        with self.Session() as s:
            return [{"device": p.device, "ip": p.ip, "kind": p.kind, "profile": p.profile,
                     "updated_at": p.updated_at.isoformat() if p.updated_at else None}
                    for p in s.scalars(select(DeviceProfile).order_by(DeviceProfile.device))]

    def benchmark_rows(self) -> list[dict]:
        with self.Session() as s:
            q = select(Run).where(Run.kind == "benchmark", Run.status == "done")
            return [self._run_dict(r, full=True) for r in s.scalars(q)]

    def delete_benchmarks(self, mode: Optional[str] = None) -> None:
        with self.Session() as s:
            q = delete(Run).where(Run.kind == "benchmark")
            if mode:
                q = q.where(Run.mode == mode)
            s.execute(q)
            s.commit()

    def stats(self) -> dict:
        with self.Session() as s:
            return {"backend": self.backend, "runs": s.scalar(select(func.count(Run.id))),
                    "samples": s.scalar(select(func.count(Sample.id))),
                    "decisions": s.scalar(select(func.count(Decision.id))),
                    "flows": s.scalar(select(func.count(FlowRecord.id)))}

    @staticmethod
    def _run_dict(r: Run, full: bool = False) -> dict:
        d = {"id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
             "finished_at": r.finished_at.isoformat() if r.finished_at else None, "kind": r.kind, "mode": r.mode,
             "scenario": r.scenario, "scheduler": r.scheduler, "seed": r.seed, "intent": r.intent,
             "status": r.status, "label": r.label, "duration_s": r.duration_s}
        if full:
            d["summary"] = r.summary
            d["meta"] = r.meta
        return d
