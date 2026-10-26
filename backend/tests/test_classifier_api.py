import os
import time

import pytest

from flowpilot.common.classes import App
from flowpilot.common.features import FEATURE_NAMES, feature_vector
from flowpilot.controller.classifier import DEFAULT_MODEL, FlowClassifier
from flowpilot.sim.apps import BulkApp, GamingApp, VideoCallApp
from flowpilot.sim.baselines import NoQoS
from flowpilot.sim.world import HomeWorld, LinkProfile


def _features_for(app_cls, **kw):
    w = HomeWorld(LinkProfile(), NoQoS(), seed=3)
    w.add_app(app_cls, "a", "pc", kw.pop("server", "game"), start=0.1, **kw)
    w.run(6.0)
    fs = next(iter(w.monitor.flows.values()))
    vec, info = feature_vector(fs, 6.0, 1, 1)
    assert len(vec) == len(FEATURE_NAMES)
    return vec


@pytest.mark.skipif(not DEFAULT_MODEL.exists(), reason="no trained model")
def test_trained_model_recognises_apps():
    clf = FlowClassifier()
    assert clf.kind == "gbdt"
    lab, conf, _ = clf.predict([_features_for(GamingApp)])[0]
    assert lab == App.GAMING and conf > 0.8
    lab, conf, _ = clf.predict([_features_for(VideoCallApp, server="call")])[0]
    assert lab == App.VIDEO_CALL
    lab, _, _ = clf.predict([_features_for(BulkApp, server="mirror", n_flows=1)])[0]
    assert lab == App.BULK


def test_rule_fallback_always_available():
    clf = FlowClassifier(model_path="/nonexistent")
    assert clf.kind == "rules"
    lab, conf, _ = clf.predict([_features_for(GamingApp)])[0]
    assert lab == App.GAMING


def test_api_live_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    from fastapi.testclient import TestClient

    from flowpilot.api.main import app

    with TestClient(app) as c:
        assert c.get("/api/health").json()["ok"]
        caps = c.get("/api/capabilities").json()
        assert "sim" in caps["modes"] and caps["presets"]
        r = c.post("/api/session/start", json={"mode": "sim", "scheduler": "flowpilot", "compare_with": "fifo",
                                               "speed": 4.0})
        assert r.status_code == 200, r.text
        with c.websocket_connect("/ws/live") as ws:
            deadline = time.time() + 30
            tick = None
            while time.time() < deadline:
                msg = ws.receive_json()
                if msg.get("type") == "tick" and msg["t"] >= 3:
                    tick = msg
                    break
            assert tick is not None
            assert set(tick["sides"]) == {"a", "b"}
            assert tick["sides"]["b"]["scheduler"] == "flowpilot"
        assert c.post("/api/session/action", json={"type": "preset", "name": "steam"}).json()["ok"]
        assert c.post("/api/session/stop").status_code == 200
        assert c.get("/api/runs").json()


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root for network namespaces")
def test_testbed_topology_connectivity():
    from flowpilot.testbed.netns import nsx
    from flowpilot.testbed.topology import Topology

    t = Topology(delay_mode="none")
    try:
        t.build()
        out = nsx("fp-dev-console", "ping -c 2 -i 0.2 -W 1 203.0.113.10")
        assert "0% packet loss" in out
    finally:
        Topology.teardown()
