"""Live-demo presets: the household that starts automatically and the one-click events.

Both the simulator worker and the kernel session use these, so a demo looks the same in
either mode.
"""
from __future__ import annotations

from ..common.classes import App
from ..bench.scenarios import AppSpec

SANDBOX_APPS = [
    AppSpec(App.GAMING, "game", "console", "game", 0.5, params={"port": 27015, "title": "CS2"}),
    AppSpec(App.VIDEO_CALL, "call", "laptop", "call", 1.0, params={"port": 3478, "title": "Teams call"}),
    AppSpec(App.STREAMING, "netflix", "tv", "cdn", 1.5, params={"top_kbps": 16000, "title": "Netflix 4K"}),
    AppSpec(App.WEB, "web", "phone", "web", 2.0, params={"title": "Browsing"}),
]

# one-click events for the dashboard: (label, description, actions)
PRESETS = {
    "steam": {
        "label": "Start Steam update", "icon": "download",
        "description": "The desktop PC starts a game update over 8 parallel TCP connections.",
        "apps": [AppSpec(App.BULK, "steam", "pc", "mirror", 0, params={"n_flows": 8, "title": "Steam update"})],
    },
    "backup": {
        "label": "Start photo backup", "icon": "upload",
        "description": "The tablet uploads its camera roll to the cloud (4 TCP uploads).",
        "apps": [AppSpec(App.UPLOAD, "backup", "tablet", "backup", 0, params={"n_flows": 4, "title": "Photo backup"})],
    },
    "sync": {
        "label": "OneDrive sync on laptop", "icon": "upload",
        "description": "The laptop that is on the call starts syncing a folder (4 uploads).",
        "apps": [AppSpec(App.UPLOAD, "sync", "laptop", "backup", 0, params={"n_flows": 4, "title": "OneDrive sync"})],
    },
    "torrent": {
        "label": "16-connection download", "icon": "download",
        "description": "A download manager on the PC opens 16 connections.",
        "apps": [AppSpec(App.BULK, "torrent", "pc", "mirror", 0, params={"n_flows": 16, "title": "Download manager"})],
    },
    "second_tv": {
        "label": "Second 4K stream", "icon": "tv",
        "description": "Someone starts another 4K stream on the tablet.",
        "apps": [AppSpec(App.STREAMING, "netflix2", "tablet", "cdn", 0, params={"top_kbps": 16000, "title": "Prime Video"})],
    },
    "congestion": {
        "label": "ISP congestion (-55%)", "icon": "alert",
        "description": "Evening congestion in the ISP access network: 50/10 -> 22/6 Mb/s.",
        "capacity": (22.0, 6.0),
    },
    "restore": {
        "label": "Restore ISP capacity", "icon": "check",
        "description": "The access network recovers to the full 50/10 Mb/s plan.",
        "capacity": (50.0, 10.0),
    },
}

STOPPABLE = ["steam", "backup", "sync", "torrent", "netflix2", "game", "call", "netflix", "web"]


def preset_catalog() -> list[dict]:
    return [{"name": k, "label": v["label"], "description": v["description"], "icon": v["icon"]}
            for k, v in PRESETS.items()]
