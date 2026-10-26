"""FlowPilot API: REST + WebSocket for the dashboard.

    uvicorn flowpilot.api.main:app --host 0.0.0.0 --port 8000
    (or: python -m flowpilot.api)

Live telemetry: WS /ws/live pushes one JSON tick per second for the running session.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..bench.runner import SCHEDULER_LABELS
from ..bench.scenarios import SCENARIO_ORDER, SCENARIOS
from ..common.classes import APP_LABELS, CLASS_INFO, INTENT_PRESETS
from ..db.store import Store, _clean
from ..sim.world import DEFAULT_DEVICES
from .presets import preset_catalog
from .session import Manager

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
FRONTEND_DIST = Path(os.environ.get("FLOWPILOT_FRONTEND", ROOT / "frontend" / "dist"))
RESULTS_DIR = BACKEND / "results"
DATA_DIR = BACKEND / "data"
MODEL_DIR = BACKEND / "flowpilot" / "models"

SIM_SCHEDULERS = ["fifo", "static_qos", "fq_codel", "cake", "cake_autorate", "flowpilot", "flowpilot_fq",
                  "flowpilot_rl"]


def _kernel_caps() -> dict:
    try:
        from ..testbed.netns import capabilities

        return capabilities()
    except Exception as exc:  # pragma: no cover
        return {"root": False, "netns": False, "qdiscs": {}, "error": repr(exc)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store()
    app.state.store = store
    app.state.manager = Manager(store)
    app.state.caps = await asyncio.get_running_loop().run_in_executor(None, _kernel_caps)
    if not store.benchmark_rows():
        from ..db.importer import import_file

        for f in sorted(RESULTS_DIR.glob("*_bench.json")):
            try:
                import_file(store, f, replace=False)
            except Exception as exc:  # pragma: no cover
                print("import failed", f, exc)
    yield
    if app.state.manager.current is not None:
        await app.state.manager.stop()


app = FastAPI(title="FlowPilot - Adaptive QoS Engine", version=__version__, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class StartReq(BaseModel):
    mode: str = Field("sim", pattern="^(sim|kernel)$")
    scenario: str = "sandbox"
    scheduler: str = "flowpilot"
    compare_with: Optional[str] = None
    seed: int = 1
    speed: float = 1.0
    intent: str = "balanced"


class ActionReq(BaseModel):
    type: str
    name: Optional[str] = None
    app_id: Optional[str] = None
    side: Optional[str] = None
    device: Optional[str] = None
    tc: Optional[int] = None
    down: Optional[float] = None
    up: Optional[float] = None
    value: Optional[float] = None


# ---------------------------------------------------------------------------- meta
@app.get("/api/health")
def health():
    st = app.state.store
    return {"ok": True, "version": __version__, "db": st.backend}


@app.get("/api/capabilities")
def capabilities():
    caps = app.state.caps
    kernel_ok = bool(caps.get("root") and caps.get("netns") and caps.get("qdiscs", {}).get("htb"))
    kernel_scheds = ["fifo", "static_qos", "flowpilot", "flowpilot_rl"]
    if caps.get("qdiscs", {}).get("fq_codel"):
        kernel_scheds.insert(2, "fq_codel")
    if caps.get("qdiscs", {}).get("cake"):
        kernel_scheds[3:3] = ["cake", "cake_autorate"]
    return {
        "kernel": caps, "kernel_available": kernel_ok,
        "modes": ["sim"] + (["kernel"] if kernel_ok else []),
        "schedulers": {"sim": SIM_SCHEDULERS, "kernel": kernel_scheds},
        "scheduler_labels": SCHEDULER_LABELS,
        "scenarios": [{"name": "sandbox", "title": "Live household (interactive)",
                       "description": "Game, Teams call, 4K Netflix and browsing; you trigger the events."}] +
                     [{"name": n, "title": SCENARIOS[n].title, "description": SCENARIOS[n].description,
                       "duration": SCENARIOS[n].duration} for n in SCENARIO_ORDER],
        "presets": preset_catalog(),
        "intents": list(INTENT_PRESETS),
        "classes": {int(k): {"name": v.name, "prio": v.htb_prio, "dscp": v.dscp, "wmm": v.wmm, "colour": v.colour,
                             "target_delay_ms": v.target_delay_ms} for k, v in CLASS_INFO.items()},
        "apps": APP_LABELS,
        "devices": [{"name": d.name, "ip": d.ip, "kind": d.kind} for d in DEFAULT_DEVICES],
    }


# ---------------------------------------------------------------------------- live session
@app.get("/api/session")
def session_info():
    return app.state.manager.info()


@app.post("/api/session/start")
async def session_start(req: StartReq):
    if req.mode == "kernel":
        caps = app.state.caps
        if not (caps.get("root") and caps.get("netns")):
            raise HTTPException(400, "kernel mode needs Linux, root and network namespaces")
    if req.scenario != "sandbox" and req.scenario not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {req.scenario}")
    try:
        return await app.state.manager.start(req.model_dump())
    except Exception as exc:
        raise HTTPException(500, f"could not start session: {exc!r}")


@app.post("/api/session/stop")
async def session_stop():
    return await app.state.manager.stop()


@app.post("/api/session/action")
async def session_action(req: ActionReq):
    try:
        return await app.state.manager.action(req.model_dump(exclude_none=True))
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    hub = app.state.manager.hub
    await hub.join(ws)
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        hub.leave(ws)
    except Exception:
        hub.leave(ws)


# ---------------------------------------------------------------------------- history
@app.get("/api/runs")
def runs(limit: int = 50, kind: Optional[str] = None):
    return app.state.store.list_runs(limit, kind)


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    r = app.state.store.get_run(run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@app.get("/api/runs/{run_id}/samples")
def run_samples(run_id: int, side: Optional[str] = None):
    return app.state.store.run_samples(run_id, side)


@app.get("/api/runs/{run_id}/decisions")
def run_decisions(run_id: int, limit: int = 300):
    return app.state.store.run_decisions(run_id, limit)


@app.get("/api/profiles")
def profiles():
    return app.state.store.profiles()


@app.get("/api/db")
def db_stats():
    return app.state.store.stats()


# ---------------------------------------------------------------------------- benchmarks
@app.get("/api/benchmarks")
def benchmarks():
    rows = app.state.store.benchmark_rows()
    out = []
    for r in rows:
        s = r.get("summary") or {}
        out.append({"id": r["id"], "mode": r["mode"], "scenario": r["scenario"], "scheduler": r["scheduler"],
                    "seed": r["seed"], "label": SCHEDULER_LABELS.get(r["scheduler"], r["scheduler"]),
                    "summary": {k: v for k, v in s.items() if k not in ("apps",)},
                    "flow_accuracy": (r.get("meta") or {}).get("flow_accuracy")})
    return out


@app.get("/api/benchmarks/{run_id}/series")
def benchmark_series(run_id: int):
    return app.state.store.run_samples(run_id)


# ---------------------------------------------------------------------------- models
def _read_json(p: Path) -> Optional[dict]:
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


@app.get("/api/models/classifier")
def classifier_card():
    card = _read_json(DATA_DIR / "classifier_report.json")
    if card is None:
        try:
            import joblib

            card = joblib.load(MODEL_DIR / "flow_classifier.joblib").get("card")
        except Exception:
            card = None
    if card is None:
        raise HTTPException(404, "no trained classifier")
    return JSONResponse(_clean(card))


@app.get("/api/models/rl")
def rl_card():
    rep = _read_json(DATA_DIR / "rl_report.json")
    if rep is None:
        raise HTTPException(404, "no RL report")
    return JSONResponse(_clean(rep))


@app.get("/api/report/figures")
def figures():
    figdir = ROOT / "docs" / "figures"
    return sorted(p.name for p in figdir.glob("*.png")) if figdir.exists() else []


# ---------------------------------------------------------------------------- frontend
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = FRONTEND_DIST / path
        if path and f.is_file():
            return FileResponse(f)
        return FileResponse(FRONTEND_DIST / "index.html")
