"""The simulated home network ("digital twin").

    Internet servers --(path delay)--> [ISP access link: capacity C(t), deep FIFO] --> [home router: QoS under test] --> devices
    devices --> [home router: QoS under test] --> [ISP access link: capacity C_up(t), deep FIFO] --(path delay)--> servers

The ISP link models what the household cannot change: a capacity that may vary (evening
congestion, 5G FWA radio conditions) and an over-sized drop-tail buffer (bufferbloat).
The router is where FlowPilot or a baseline runs. Exactly like real SQM, a router can
only keep the queue (and therefore latency) under its own control by shaping slightly
below the true bottleneck capacity.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..common.features import FlowMonitor
from .engine import Sim
from .net import K_TCP_ACK, K_UDP, Flow, Link, Packet
from .qdisc import FIFO

LAN_RATE = 1e9


@dataclass
class LinkProfile:
    down_mbps: float = 50.0
    up_mbps: float = 10.0
    isp_buffer_ms_down: float = 250.0  # deep buffers of cable/DSL/FWA gear at nominal rate
    isp_buffer_ms_up: float = 500.0
    access_delay_ms: float = 2.0
    lan_delay_ms: float = 0.5

    def isp_buffer_bytes(self, up: bool) -> int:
        rate = (self.up_mbps if up else self.down_mbps) * 1e6
        ms = self.isp_buffer_ms_up if up else self.isp_buffer_ms_down
        return int(rate / 8 * ms / 1000)


@dataclass
class Device:
    name: str
    ip: str
    kind: str


@dataclass
class Server:
    name: str
    ip: str
    delay_ms: float  # one-way path delay beyond the ISP edge


DEFAULT_DEVICES = [
    Device("console", "192.168.1.10", "Game console"),
    Device("laptop", "192.168.1.11", "Work laptop"),
    Device("tv", "192.168.1.12", "Smart TV"),
    Device("pc", "192.168.1.13", "Desktop PC"),
    Device("phone", "192.168.1.14", "Phone"),
    Device("tablet", "192.168.1.15", "Tablet"),
]

DEFAULT_SERVERS = [
    Server("game", "203.0.113.10", 12.0),
    Server("call", "203.0.113.11", 20.0),
    Server("cdn", "203.0.113.12", 4.0),
    Server("mirror", "203.0.113.13", 18.0),
    Server("web", "203.0.113.14", 10.0),
    Server("backup", "203.0.113.15", 25.0),
    Server("reflector", "203.0.113.53", 3.0),
]


class RouterQoS:
    """A queue-management policy running on the home router (baseline or FlowPilot)."""

    name = "base"
    label = "base"

    def install(self, world: "HomeWorld") -> None:
        raise NotImplementedError

    def uninstall(self, world: "HomeWorld") -> None:
        """Release resources (probes) before the router is switched to another policy."""
        probe = getattr(self, "probe", None)
        if probe is not None:
            probe.stop()

    def tick(self, world: "HomeWorld", now: float) -> None:
        pass

    def status(self) -> dict:
        return {"scheduler": self.name}


@dataclass
class TimelineEvent:
    t: float
    fn: Callable[[], None]
    label: str = ""


class HomeWorld:
    def __init__(self, link: LinkProfile, qos: RouterQoS, seed: int = 1,
                 devices: Optional[list[Device]] = None, servers: Optional[list[Server]] = None) -> None:
        self.sim = Sim()
        self.link = link
        self.seed = seed
        self.rng = random.Random(seed)
        self.devices = {d.name: d for d in (devices or DEFAULT_DEVICES)}
        self.servers = {s.name: s for s in (servers or DEFAULT_SERVERS)}
        self.flows: dict[int, Flow] = {}
        self.flow_by_key: dict[tuple, Flow] = {}
        self._fid = itertools.count(1)
        self._ports = itertools.count(40000)
        self.apps: dict[str, object] = {}
        self.capacity_down = link.down_mbps * 1e6
        self.capacity_up = link.up_mbps * 1e6
        acc = link.access_delay_ms / 1000
        lan = link.lan_delay_ms / 1000
        self.lan_delay = lan
        # ------------------------------------------------------------------ links
        self.isp_down = Link(self.sim, "isp_down", self.capacity_down, acc,
                             FIFO(limit_bytes=link.isp_buffer_bytes(False), limit_pkts=None),
                             next_hop=self._router_down_in)
        self.router_down = Link(self.sim, "router_down", LAN_RATE, lan, FIFO(limit_pkts=1000),
                                next_hop=self._deliver_down)
        self.isp_up = Link(self.sim, "isp_up", self.capacity_up, 0.0,
                           FIFO(limit_bytes=link.isp_buffer_bytes(True), limit_pkts=None),
                           next_hop=self._deliver_up)
        self.router_up = Link(self.sim, "router_up", LAN_RATE, acc, FIFO(limit_pkts=1000),
                              next_hop=self.isp_up.arrive)
        # ------------------------------------------------------------------ router-side observation
        self.monitor = FlowMonitor()
        self.router_down.on_ingress = self._observe
        self.router_up.on_ingress = self._observe
        self.probe_handlers: list[Callable[[Packet], None]] = []
        self.qos = qos
        qos.install(self)
        self.timeline: list[TimelineEvent] = []
        self.event_log: list[dict] = []

    # ---------------------------------------------------------------------- flows
    def new_flow(self, proto: str, device: str, server: str, wan_port: int, lan_port: Optional[int],
                 app_type: str, app_id: str) -> Flow:
        dev = self.devices[device]
        srv = self.servers[server]
        f = Flow(next(self._fid), proto, dev.ip, lan_port or next(self._ports), srv.ip, wan_port,
                 device, app_type, app_id, srv.delay_ms / 1000, start=self.sim.now)
        self.flows[f.fid] = f
        self.flow_by_key[f.key] = f
        return f

    def base_rtt_ms(self, f: Flow) -> float:
        return 2 * (f.prop_delay * 1000 + self.link.access_delay_ms + self.link.lan_delay_ms)

    # ---------------------------------------------------------------------- packet paths
    def send_down(self, pkt: Packet) -> None:
        self.sim.after(pkt.flow.prop_delay, self.isp_down.arrive, pkt)

    def send_up(self, pkt: Packet) -> None:
        self.sim.after(self.lan_delay, self.router_up.arrive, pkt)

    def _router_down_in(self, pkt: Packet) -> None:
        if pkt.flow.app_type == "probe":
            for h in self.probe_handlers:
                h(pkt)
            return
        self.router_down.arrive(pkt)

    def _deliver_down(self, pkt: Packet) -> None:
        cb = pkt.flow.recv_down
        if cb is not None:
            cb(pkt)

    def _deliver_up(self, pkt: Packet) -> None:
        cb = pkt.flow.recv_up
        if cb is not None:
            self.sim.after(pkt.flow.prop_delay, cb, pkt)

    def _observe(self, pkt: Packet, now: float) -> None:
        f = pkt.flow
        if f.app_type == "probe":
            return
        self.monitor.observe(f.key, f.proto, f.lan_ip, f.wan_ip, pkt.up, pkt.size, now)

    # ---------------------------------------------------------------------- capacity
    def set_qos(self, qos: RouterQoS) -> None:
        """Hot-swap the router's queue management (live A/B demos)."""
        self.qos.uninstall(self)
        for f in self.flows.values():
            f.tc = 0
        self.qos = qos
        qos.install(self)

    def set_capacity(self, down_mbps: Optional[float] = None, up_mbps: Optional[float] = None) -> None:
        if down_mbps is not None:
            self.capacity_down = down_mbps * 1e6
            self.isp_down.set_rate(self.capacity_down)
        if up_mbps is not None:
            self.capacity_up = up_mbps * 1e6
            self.isp_up.set_rate(self.capacity_up)

    # ---------------------------------------------------------------------- apps
    def add_app(self, cls, app_id: str, device: str, server: str, start: float = 0.0,
                stop: Optional[float] = None, **params):
        app = cls(self, app_id, device, server, random.Random(self.rng.random()), **params)
        self.apps[app_id] = app
        self.at(start, app.start, f"start {app_id}")
        if stop is not None:
            self.at(stop, app.stop, f"stop {app_id}")
        return app

    def at(self, t: float, fn: Callable[[], None], label: str = "") -> None:
        def run(fn=fn, label=label):
            if label:
                self.event_log.append({"t": round(self.sim.now, 3), "event": label})
            fn()

        self.sim.at(t, run)

    def run(self, until: float, step: float = 0.1, on_step: Optional[Callable[[float], None]] = None) -> None:
        t = self.sim.now
        while t < until - 1e-9:
            t = min(until, t + step)
            self.sim.run_until(t)
            self.qos.tick(self, t)
            if on_step is not None:
                on_step(t)


# ============================================================================ probes
class ReflectorProbe:
    """Router -> public reflector -> router latency probes (both one-way delays).

    This is the signal an autorate controller uses: if the probe delay rises while the
    router's own queues are short, the queue is building at the ISP, i.e. the shaper is
    set above the current true capacity.
    """

    def __init__(self, world: HomeWorld, hz: float = 20.0) -> None:
        self.w = world
        self.hz = hz
        self.flow = world.new_flow("udp", "pc", "reflector", 7, 50999, "probe", "probe")
        self.flow.lan_ip = "192.168.1.1"  # the router itself
        self.flow.rekey()
        self.flow.recv_up = self._reflect
        world.probe_handlers.append(self._back)
        self.seq = 0
        self.samples: list[tuple[float, float, float]] = []  # (t, owd_up_ms, owd_down_ms)
        self.sent = 0
        self.recv = 0
        self.active = True
        world.sim.after(0.01, self._tick)

    def stop(self) -> None:
        self.active = False
        if self._back in self.w.probe_handlers:
            self.w.probe_handlers.remove(self._back)

    def _tick(self) -> None:
        if not self.active:
            return
        p = Packet(self.flow, 64, K_UDP, True, self.w.sim.now, seq=self.seq)
        self.seq += 1
        self.sent += 1
        # the probe leaves the router directly on the WAN side
        self.w.router_up.arrive(p)
        self.w.sim.after(1.0 / self.hz, self._tick)

    def _reflect(self, pkt: Packet) -> None:
        now = self.w.sim.now
        back = Packet(self.flow, 64, K_UDP, False, pkt.ts, seq=pkt.seq, payload=now)
        self.w.send_down(back)

    def _back(self, pkt: Packet) -> None:
        now = self.w.sim.now
        t_reflect = pkt.payload
        self.recv += 1
        self.samples.append((now, (t_reflect - pkt.ts) * 1000, (now - t_reflect) * 1000))

    def drain(self) -> list[tuple[float, float, float]]:
        out = self.samples
        self.samples = []
        return out


def is_pure_ack(pkt: Packet) -> bool:
    return pkt.kind == K_TCP_ACK
