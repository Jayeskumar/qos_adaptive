"""Benchmark scenarios: realistic household traffic mixes plus congestion events.

Scenarios are declarative (applications + timed capacity changes) so the *same* scenario
drives both the simulator and the real-kernel Linux testbed.

The household: game console, work laptop, smart TV, desktop PC, phone, tablet on a
50/10 Mb/s plan (unless stated). Events are what households actually experience: a big
game update starting, a tablet backing up photos, evening congestion in the ISP's access
network, and the capacity swings of 5G fixed-wireless access.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from ..common.classes import App
from ..sim.world import LinkProfile


@dataclass
class AppSpec:
    app_type: str
    app_id: str
    device: str
    server: str
    start: float
    stop: Optional[float] = None
    params: dict = field(default_factory=dict)


@dataclass
class CapEvent:
    t: float
    down_mbps: float
    up_mbps: float
    label: str = ""


@dataclass
class Scenario:
    name: str
    title: str
    description: str
    duration: float
    link: LinkProfile
    apps: list
    caps: list = field(default_factory=list)
    marks: list = field(default_factory=list)  # [(t, label)] annotations for plots

    def capacity_at(self, t: float) -> tuple[float, float]:
        d, u = self.link.down_mbps, self.link.up_mbps
        for ev in self.caps:
            if ev.t <= t:
                d, u = ev.down_mbps, ev.up_mbps
        return d, u


def family(stream: bool = True, web: bool = True, game: bool = True, call: bool = True,
           game_port: int = 27015, call_port: int = 3478) -> list:
    out = []
    if game:
        out.append(AppSpec(App.GAMING, "game", "console", "game", 1.0, params={"port": game_port, "title": "CS2"}))
    if call:
        out.append(AppSpec(App.VIDEO_CALL, "call", "laptop", "call", 2.0, params={"port": call_port, "title": "Teams call"}))
    if stream:
        out.append(AppSpec(App.STREAMING, "netflix", "tv", "cdn", 3.0, params={"top_kbps": 16000, "title": "Netflix 4K"}))
    if web:
        out.append(AppSpec(App.WEB, "web", "phone", "web", 4.0, params={"title": "Browsing"}))
    return out


def fwa_trace(seed: int, duration: float, step: float = 1.0, lo: float = 15.0, hi: float = 60.0,
              mean: float = 38.0) -> list[CapEvent]:
    """5G FWA-like capacity: AR(1) in log space plus occasional deep fades (uplink = 1/5)."""
    rng = random.Random(seed * 7919 + 17)
    x = math.log(mean)
    out = []
    fade = 0.0
    t = 0.0
    while t < duration:
        x = 0.85 * x + 0.15 * math.log(mean) + rng.gauss(0, 0.12)
        if fade <= 0 and rng.random() < 0.03:
            fade = rng.uniform(3, 8)
        c = math.exp(x) * (0.45 if fade > 0 else 1.0)
        c = max(lo * (0.6 if fade > 0 else 1.0), min(hi, c))
        fade -= step
        out.append(CapEvent(t, round(c, 2), round(max(3.0, c / 5.0), 2)))
        t += step
    return out


def _mk(seed: int = 1) -> dict:
    sc = {}
    sc["evening_rush"] = Scenario(
        "evening_rush", "Evening rush",
        "Game + Teams call + 4K Netflix + web browsing; at t=20 s a Steam update opens 8 TCP connections.",
        100.0, LinkProfile(),
        family() + [AppSpec(App.BULK, "steam", "pc", "mirror", 20.0, params={"n_flows": 8, "title": "Steam update"})],
        marks=[(20.0, "Steam update starts")])
    sc["upload_storm"] = Scenario(
        "upload_storm", "Upload storm",
        "Game + call + web; at 15 s a tablet backs up photos (4 uploads), at 30 s a download starts.",
        90.0, LinkProfile(),
        family(stream=False) + [
            AppSpec(App.UPLOAD, "backup", "tablet", "backup", 15.0, params={"n_flows": 4, "title": "Photo backup"}),
            AppSpec(App.BULK, "steam", "pc", "mirror", 30.0, params={"n_flows": 4, "title": "Steam update"})],
        marks=[(15.0, "Photo backup"), (30.0, "Download")])
    sc["wfh_same_laptop"] = Scenario(
        "wfh_same_laptop", "Work-from-home sync",
        "The laptop is on a Teams call while OneDrive syncs a folder (4 uploads) from the same laptop.",
        80.0, LinkProfile(),
        [AppSpec(App.VIDEO_CALL, "call", "laptop", "call", 1.0, params={"title": "Teams call"}),
         AppSpec(App.GAMING, "game", "console", "game", 2.0, params={"title": "CS2"}),
         AppSpec(App.UPLOAD, "sync", "laptop", "backup", 15.0, params={"n_flows": 4, "title": "OneDrive sync"}),
         AppSpec(App.WEB, "web", "phone", "web", 3.0)],
        marks=[(15.0, "OneDrive sync")])
    sc["isp_congestion"] = Scenario(
        "isp_congestion", "ISP congestion",
        "Evening-rush traffic; from 45 s to 85 s the ISP capacity drops from 50/10 to 22/6 Mb/s.",
        110.0, LinkProfile(),
        family() + [AppSpec(App.BULK, "steam", "pc", "mirror", 15.0, params={"n_flows": 8, "title": "Steam update"})],
        caps=[CapEvent(45.0, 22.0, 6.0, "ISP congestion: 22/6 Mb/s"), CapEvent(85.0, 50.0, 10.0, "ISP recovers")],
        marks=[(45.0, "Capacity drop"), (85.0, "Recovery")])
    sc["download_hog"] = Scenario(
        "download_hog", "Download hog (fairness)",
        "A PC opens 16 connections while a tablet downloads with 1; plus a call and 4K Netflix.",
        80.0, LinkProfile(),
        [AppSpec(App.VIDEO_CALL, "call", "laptop", "call", 1.0, params={"title": "Teams call"}),
         AppSpec(App.STREAMING, "netflix", "tv", "cdn", 2.0, params={"top_kbps": 16000, "title": "Netflix 4K"}),
         AppSpec(App.BULK, "torrent", "pc", "mirror", 10.0, params={"n_flows": 16, "title": "16-connection download"}),
         AppSpec(App.BULK, "update", "tablet", "mirror", 12.0, params={"n_flows": 1, "title": "OS update"})],
        marks=[(10.0, "16-conn download"), (12.0, "1-conn download")])
    sc["fwa_fading"] = Scenario(
        "fwa_fading", "5G FWA fading",
        "Family traffic over 5G fixed-wireless: capacity wanders 15-60 Mb/s with deep fades.",
        120.0, LinkProfile(down_mbps=60.0, up_mbps=12.0),
        family() + [AppSpec(App.BULK, "steam", "pc", "mirror", 10.0, params={"n_flows": 4, "title": "Steam update"})],
        caps=fwa_trace(seed, 125.0))
    return sc


SCENARIOS = _mk()
SCENARIO_ORDER = ["evening_rush", "upload_storm", "wfh_same_laptop", "isp_congestion", "download_hog", "fwa_fading"]


def get(name: str, seed: int = 1) -> Scenario:
    if name == "fwa_fading" and seed != 1:
        return _mk(seed)[name]
    return SCENARIOS[name]


def build_sim(world, sc: Scenario) -> None:
    """Instantiate a scenario in the simulator."""
    from ..sim.apps import BulkApp, GamingApp, StreamingApp, VideoCallApp, WebApp

    cls = {App.GAMING: GamingApp, App.VIDEO_CALL: VideoCallApp, App.STREAMING: StreamingApp,
           App.WEB: WebApp, App.BULK: BulkApp, App.UPLOAD: BulkApp}
    for a in sc.apps:
        params = dict(a.params)
        if a.app_type == App.UPLOAD:
            params["upload"] = True
        world.add_app(cls[a.app_type], a.app_id, a.device, a.server, start=a.start, stop=a.stop, **params)
    for ev in sc.caps:
        world.at(ev.t, lambda ev=ev: world.set_capacity(ev.down_mbps, ev.up_mbps), ev.label)
