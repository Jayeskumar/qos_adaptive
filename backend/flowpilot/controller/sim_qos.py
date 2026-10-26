"""Adapter that runs the FlowPilot brain against the simulator's data plane.

It builds the same HTB tree that `flowpilot.testbed.dataplane` programs into the Linux
kernel, feeds the brain with router-observable telemetry only (flow monitor, HTB class
counters, reflector probes) and applies the brain's plans every control period.
"""
from __future__ import annotations

from typing import Optional

from ..common.classes import CLASS_INFO, TC
from ..sim.net import K_TCP_ACK, Packet
from ..sim.qdisc import FIFO, HTB, FQCoDel
from ..sim.world import LAN_RATE, HomeWorld, ReflectorProbe, RouterQoS
from .autorate import DelayAutorate
from .brain import BULK_INNER, ClassObs, FlowPilotBrain, FlowView, Observation
from .classifier import FlowClassifier

ACK_CLASS = TC.INTERACTIVE


class FlowPilotQoS(RouterQoS):
    name = "flowpilot"
    label = "FlowPilot (AI)"

    def __init__(self, classifier: Optional[FlowClassifier] = None, autorate=None, intent: str = "balanced",
                 leaf: str = "fifo", rate_factor: float = 0.95, control_interval: float = 0.5,
                 feature_interval: float = 1.0, name: Optional[str] = None, label: Optional[str] = None) -> None:
        self.clf = classifier or FlowClassifier()
        self.autorate = autorate if autorate is not None else DelayAutorate(max_frac=0.95)
        self.intent = intent
        self.leaf_kind = leaf
        self.rate_factor = rate_factor
        self.control_interval = control_interval
        self.feature_interval = feature_interval
        if name:
            self.name = name
        if label:
            self.label = label
        self.world: Optional[HomeWorld] = None
        self.htb: dict[str, HTB] = {}
        self.brain: Optional[FlowPilotBrain] = None
        self._last_ctrl = -1.0
        self._last_auto = 0.0
        self._last_feat = -1.0
        self.decisions: list[dict] = []

    # ---------------------------------------------------------------- data plane
    def _leaf(self):
        return FQCoDel() if self.leaf_kind == "fq_codel" else FIFO(limit_bytes=64 * 1514, limit_pkts=None)

    def _build(self, R: float) -> HTB:
        h = HTB(R, default_cid=int(TC.INTERACTIVE))
        for tc in (TC.REALTIME, TC.CONFERENCING, TC.INTERACTIVE, TC.STREAMING):
            info = CLASS_INFO[tc]
            h.add_class(int(tc), 1, 0.05 * R, R, info.htb_prio, self._leaf(), info.name)
        h.add_inner(BULK_INNER, 1, 0.05 * R, R, CLASS_INFO[TC.BULK].htb_prio, "Bulk")
        for slot in range(1, 10):
            h.add_class(BULK_INNER + slot, BULK_INNER, 8_000.0, R, CLASS_INFO[TC.BULK].htb_prio,
                        self._leaf(), f"Bulk/{slot}")

        def classify(pkt: Packet) -> int:
            f = pkt.flow
            if f.app_type == "probe":
                return int(TC.REALTIME)
            if pkt.kind == K_TCP_ACK:
                return int(ACK_CLASS)
            if f.proto == "udp" and f.wan_port == 53:
                return int(TC.REALTIME)
            return f.tc or int(TC.INTERACTIVE)

        h.classify = classify
        return h

    def install(self, world: HomeWorld) -> None:
        self.world = world
        down = world.link.down_mbps * 1e6 * self.rate_factor
        up = world.link.up_mbps * 1e6 * self.rate_factor
        self.htb = {"down": self._build(down), "up": self._build(up)}
        world.router_down.set_rate(LAN_RATE)
        world.router_up.set_rate(LAN_RATE)
        world.router_down.set_qdisc(self.htb["down"])
        world.router_up.set_qdisc(self.htb["up"])
        self.probe = ReflectorProbe(world)
        self.autorate.reset(down, up, world.link.down_mbps * 1e6, world.link.up_mbps * 1e6)
        devices = {d.ip: d.name for d in world.devices.values()}
        self.brain = FlowPilotBrain(devices, self.clf, intent=self.intent)
        self._last_auto = self._last_ctrl = self._last_feat = world.sim.now

    # ---------------------------------------------------------------- control loop
    def tick(self, world: HomeWorld, now: float) -> None:
        if now - self._last_auto >= self.autorate.interval - 1e-9:
            dt = now - self._last_auto
            self._last_auto = now
            rd, ru = self.autorate.update(now, self.probe.drain(), world.router_down.tx_bytes,
                                          world.router_up.tx_bytes, dt)
            self.htb["down"].set_root_rate(rd)
            self.htb["up"].set_root_rate(ru)
            for ev in self.autorate.drain_events():
                d = {"t": round(now, 2), "kind": "autorate",
                     "message": (f"Bufferbloat on the {'downlink' if ev['dir'] == 'down' else 'uplink'} "
                                 f"(+{ev['delta_ms']:.0f} ms): ISP capacity fell, shaper "
                                 f"{ev['from'] / 1e6:.1f} -> {ev['to'] / 1e6:.1f} Mb/s"),
                     "details": ev}
                self.brain.decisions.append(d)
                self.decisions.append(d)
        if now - self._last_ctrl >= self.control_interval - 1e-9:
            self._last_ctrl = now
            obs = self._observe(world, now)
            acts = self.brain.step(obs)
            self._apply(world, acts)
            self.decisions.extend(acts.decisions)

    def _observe(self, world: HomeWorld, now: float) -> Observation:
        want_feat = now - self._last_feat >= self.feature_interval - 1e-9
        if want_feat:
            self._last_feat = now
        mon = world.monitor
        per_host, per_dst = mon.host_counts(now) if want_feat else ({}, {})
        flows = []
        from ..common.features import feature_vector

        for fs in mon.flows.values():
            if now - fs.last_ts > 2.0:
                continue
            fv = FlowView(fs.key, fs.proto, fs.lan_ip, fs.wan_ip, fs.first_ts, fs.last_ts,
                          fs.up.bytes, fs.down.bytes)
            if want_feat and fs.pkts >= 6:
                fv.vec, fv.info = feature_vector(fs, now, per_host.get(fs.lan_ip, 1),
                                                 per_dst.get((fs.lan_ip, fs.wan_ip), 1))
            flows.append(fv)
        mon.expire(now)
        classes = {}
        for d, h in self.htb.items():
            classes[d] = {c.cid: ClassObs(c.sent_bytes, c.leaf.backlog_bytes, c.leaf.drops)
                          for c in h.classes.values() if c.leaf is not None}
        cap = {"down": self.htb["down"].root.rate, "up": self.htb["up"].root.rate}
        return Observation(now, flows, classes, cap)

    def _apply(self, world: HomeWorld, acts) -> None:
        if acts.assign:
            by_key = world.flow_by_key
            for key, cid in acts.assign.items():
                f = by_key.get(key)
                if f is not None:
                    f.tc = cid
        for d, plan in acts.plans.items():
            h = self.htb[d]
            for cid, p in plan.inner.items():
                h.change_class(cid, rate=p["rate"], ceil=p["ceil"], prio=p["prio"])
            for cid, p in plan.leaves.items():
                if cid not in h.classes:
                    continue
                h.change_class(cid, rate=p["rate"], ceil=p["ceil"], prio=p["prio"], burst=p.get("burst"))
                leaf = h.classes[cid].leaf
                if isinstance(leaf, FIFO):
                    leaf.set_limit(limit_bytes=int(p["limit_bytes"]), limit_pkts=None)

    def status(self) -> dict:
        return {"scheduler": self.name, "autorate": self.autorate.status(),
                "classes": self.brain.class_status if self.brain else {}}
