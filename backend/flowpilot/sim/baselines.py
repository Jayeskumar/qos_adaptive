"""Baseline router queue management the FlowPilot controller is compared against.

fifo          "standard" router: no shaping, drop-tail everywhere. The queue builds in the
              ISP's deep buffer -> bufferbloat. (Most ISP-supplied CPE behave like this.)
static_qos    classic "gaming router" QoS: HTB shaper fixed at 95 % of the *plan* rate,
              port-based priority rules, drop-tail leaves. No knowledge of the true
              capacity, of encrypted apps on port 443, or of user intent.
fq_codel      OpenWrt SQM (simplest.qos): shaper at 95 % of plan rate + FQ-CoDel.
cake          SQM with CAKE-style per-host fairness + flow queueing + CoDel AQM.
cake_autorate CAKE + a delay-based autorate loop (cake-autorate style) that tracks
              capacity changes: the strongest open-source competitor.
"""
from __future__ import annotations

from typing import Optional

from ..common.classes import TC
from .net import K_TCP_ACK, Packet
from .qdisc import FIFO, HTB, CakeLite, FQCoDel
from .world import LAN_RATE, HomeWorld, ReflectorProbe, RouterQoS


class NoQoS(RouterQoS):
    name = "fifo"
    label = "Standard router (FIFO)"

    def install(self, world: HomeWorld) -> None:
        world.router_down.set_rate(LAN_RATE)
        world.router_up.set_rate(LAN_RATE)
        world.router_down.set_qdisc(FIFO(limit_pkts=1000))
        world.router_up.set_qdisc(FIFO(limit_pkts=1000))


class SqmQoS(RouterQoS):
    def __init__(self, kind: str = "fq_codel", rate_factor: float = 0.95, autorate=None) -> None:
        self.kind = kind
        self.rate_factor = rate_factor
        self.autorate = autorate
        self.name = kind if autorate is None else f"{kind}_autorate"
        self.label = {"fq_codel": "SQM fq_codel", "cake": "SQM CAKE"}[kind]
        if autorate is not None:
            self.label = "CAKE + autorate" if kind == "cake" else "fq_codel + autorate"
        self.probe: Optional[ReflectorProbe] = None
        self._last = 0.0
        self.world: Optional[HomeWorld] = None

    def _mk(self):
        return CakeLite() if self.kind == "cake" else FQCoDel()

    def install(self, world: HomeWorld) -> None:
        self.world = world
        down = world.link.down_mbps * 1e6 * self.rate_factor
        up = world.link.up_mbps * 1e6 * self.rate_factor
        world.router_down.set_rate(down)
        world.router_up.set_rate(up)
        world.router_down.set_qdisc(self._mk())
        world.router_up.set_qdisc(self._mk())
        if self.autorate is not None:
            self.probe = ReflectorProbe(world)
            self.autorate.reset(down, up, world.link.down_mbps * 1e6, world.link.up_mbps * 1e6)

    def tick(self, world: HomeWorld, now: float) -> None:
        if self.autorate is None or now - self._last < self.autorate.interval - 1e-9:
            return
        dt = now - self._last
        self._last = now
        down_tx = world.router_down.tx_bytes
        up_tx = world.router_up.tx_bytes
        rd, ru = self.autorate.update(now, self.probe.drain(), down_tx, up_tx, dt)
        world.router_down.set_rate(rd)
        world.router_up.set_rate(ru)

    def status(self) -> dict:
        return {"scheduler": self.name, "shaper_down": self.world.router_down.rate,
                "shaper_up": self.world.router_up.rate}


# port rules of a typical consumer "gaming / VoIP QoS" router
STATIC_HIGH_UDP = [(3074, 3074), (27000, 27100), (3478, 3481), (8801, 8810), (19302, 19309),
                   (5060, 5061), (53, 53)]


def static_class(pkt: Packet) -> int:
    f = pkt.flow
    if f.app_type == "probe":
        return TC.REALTIME
    if pkt.kind == K_TCP_ACK and pkt.up:
        return TC.REALTIME  # ACK prioritisation on the uplink
    if f.proto == "udp":
        for lo, hi in STATIC_HIGH_UDP:
            if lo <= f.wan_port <= hi:
                return TC.REALTIME
    return TC.INTERACTIVE


class StaticQoS(RouterQoS):
    name = "static_qos"
    label = "Static QoS (port rules)"

    def __init__(self, rate_factor: float = 0.95) -> None:
        self.rate_factor = rate_factor
        self.world: Optional[HomeWorld] = None

    def _build(self, rate: float) -> HTB:
        h = HTB(rate, default_cid=TC.INTERACTIVE)
        h.add_class(TC.REALTIME, 1, 0.20 * rate, rate, 0, FIFO(limit_pkts=100), "High")
        h.add_class(TC.INTERACTIVE, 1, 0.70 * rate, rate, 1, FIFO(limit_pkts=300), "Normal")
        h.add_class(TC.BULK, 1, 0.10 * rate, rate, 2, FIFO(limit_pkts=300), "Low")
        h.classify = static_class
        return h

    def install(self, world: HomeWorld) -> None:
        self.world = world
        world.router_down.set_rate(LAN_RATE)
        world.router_up.set_rate(LAN_RATE)
        world.router_down.set_qdisc(self._build(world.link.down_mbps * 1e6 * self.rate_factor))
        world.router_up.set_qdisc(self._build(world.link.up_mbps * 1e6 * self.rate_factor))


def make_baseline(name: str) -> RouterQoS:
    from ..controller.autorate import DelayAutorate  # local import avoids a cycle

    if name == "fifo":
        return NoQoS()
    if name == "static_qos":
        return StaticQoS()
    if name == "fq_codel":
        return SqmQoS("fq_codel")
    if name == "cake":
        return SqmQoS("cake")
    if name == "cake_autorate":
        return SqmQoS("cake", autorate=DelayAutorate())
    raise KeyError(name)
