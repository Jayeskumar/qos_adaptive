"""Application types, traffic classes and their service requirements.

The vocabulary here is shared by the simulator, the Linux testbed, the controller
and the dashboard, so that a "video_call" means the same thing everywhere.

Traffic classes follow the spirit of RFC 4594 (DiffServ service classes) and the
RFC 8325 Wi-Fi mapping, collapsed to the five classes that matter in a home:

  REALTIME      game state, VoIP, latency probes, DNS, pure TCP ACKs (uplink)
  CONFERENCING  interactive video calls (Zoom / Teams / Meet)
  INTERACTIVE   web browsing, API calls, and *unclassified new flows*
  STREAMING     buffered video on demand (Netflix / YouTube / Prime)
  BULK          downloads, game updates, cloud backup (RFC 8622 "lower effort")
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class App:
    GAMING = "gaming"
    VIDEO_CALL = "video_call"
    STREAMING = "streaming"
    WEB = "web"
    BULK = "bulk"
    UPLOAD = "upload"


# Order matters: it is the label order used by the ML classifier.
APP_TYPES: list[str] = [App.GAMING, App.VIDEO_CALL, App.STREAMING, App.WEB, App.BULK, App.UPLOAD]

APP_LABELS: dict[str, str] = {
    App.GAMING: "Online gaming",
    App.VIDEO_CALL: "Video call",
    App.STREAMING: "Video streaming",
    App.WEB: "Web browsing",
    App.BULK: "Bulk download",
    App.UPLOAD: "Cloud backup",
}


class TC(IntEnum):
    """Traffic class ids. The value doubles as the HTB minor class id (1:<value>)."""

    REALTIME = 10
    CONFERENCING = 20
    INTERACTIVE = 30
    STREAMING = 40
    BULK = 50


@dataclass(frozen=True)
class ClassInfo:
    tc: TC
    name: str
    htb_prio: int  # lower = served first when borrowing spare bandwidth
    target_delay_ms: float  # queueing-delay budget used to size the leaf buffer
    min_buffer_pkts: int
    dscp: int  # DSCP written on egress so Wi-Fi WMM / ISP NQB can honour the class
    wmm: str  # RFC 8325 access category the DSCP maps to
    elastic: bool  # elastic (capacity-seeking) vs inelastic (application-limited)
    colour: str


CLASS_INFO: dict[TC, ClassInfo] = {
    TC.REALTIME: ClassInfo(TC.REALTIME, "Real-time", 0, 5.0, 8, 45, "AC_VO", False, "#e5484d"),
    TC.CONFERENCING: ClassInfo(TC.CONFERENCING, "Conferencing", 1, 15.0, 12, 34, "AC_VI", False, "#f5a524"),
    TC.INTERACTIVE: ClassInfo(TC.INTERACTIVE, "Interactive", 2, 15.0, 16, 18, "AC_BE", True, "#3e9bf5"),
    TC.STREAMING: ClassInfo(TC.STREAMING, "Streaming", 3, 20.0, 20, 26, "AC_BE", True, "#8e4ec6"),
    TC.BULK: ClassInfo(TC.BULK, "Bulk", 4, 20.0, 20, 1, "AC_BK", True, "#30a46c"),
}

APP_TO_CLASS: dict[str, TC] = {
    App.GAMING: TC.REALTIME,
    App.VIDEO_CALL: TC.CONFERENCING,
    App.WEB: TC.INTERACTIVE,
    App.STREAMING: TC.STREAMING,
    App.BULK: TC.BULK,
    App.UPLOAD: TC.BULK,
}

DEFAULT_CLASS = TC.INTERACTIVE  # where a brand-new, not yet classified flow starts

# Service-level objectives used for "% of time within SLO" reporting.
# Latency SLOs are on the *added* (queueing) latency an app experiences, i.e. what
# the home network is responsible for; base path delay is outside its control.
SLO = {
    App.GAMING: {"added_latency_p95_ms": 30.0, "loss_pct": 1.0},
    App.VIDEO_CALL: {"added_latency_p95_ms": 50.0, "loss_pct": 2.0},
    App.STREAMING: {"rebuffer_ratio": 0.0},
    App.WEB: {"plt_p50_s": 2.0},
}

# Intent presets the household can pick in the dashboard. They scale the NUM weights
# used by the allocator; they never disable the anti-starvation floors.
INTENT_PRESETS: dict[str, dict[TC, float]] = {
    "balanced": {TC.REALTIME: 8, TC.CONFERENCING: 8, TC.INTERACTIVE: 4, TC.STREAMING: 4, TC.BULK: 1},
    "gaming": {TC.REALTIME: 12, TC.CONFERENCING: 6, TC.INTERACTIVE: 4, TC.STREAMING: 3, TC.BULK: 1},
    "work_from_home": {TC.REALTIME: 6, TC.CONFERENCING: 12, TC.INTERACTIVE: 6, TC.STREAMING: 2, TC.BULK: 1},
    "movie_night": {TC.REALTIME: 6, TC.CONFERENCING: 6, TC.INTERACTIVE: 3, TC.STREAMING: 8, TC.BULK: 1},
}
