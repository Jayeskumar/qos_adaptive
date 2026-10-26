"""FlowPilot brain: the closed control loop, independent of the data plane.

Every control period (500 ms) it runs  observe -> classify -> estimate -> allocate -> act:

  1. observe   per-flow byte counters + behavioural features from the router's flow
               monitor, per-class queue statistics, current shaper capacity (autorate)
  2. classify  ML classifier with hysteresis (a label changes only after two consistent,
               confident predictions); RFC 9956-style traffic protection demotes flows
               that exceed their class budget
  3. estimate  per-class offered load, peak-hold demand, saturation (is the class using
               all it is given?), number of active video streams
  4. allocate  NUM allocation (inelastic first, then weighted water-filling) and
               per-device fairness inside the bulk class
  5. act       a plan for each direction (HTB root rate, class rate/ceil/prio, buffer
               sizes) plus flow->class assignments, applied by the data-plane adapter
               (simulator or Linux tc/nftables)
Every decision is logged in plain language for the dashboard and the database.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from ..common.classes import APP_LABELS, APP_TO_CLASS, CLASS_INFO, DEFAULT_CLASS, INTENT_PRESETS, TC
from .allocator import ClassDemand, allocate
from .classifier import FlowClassifier

BULK_INNER = 50
DIRS = ("down", "up")
BULK_RTT_EST = 0.050  # typical RTT to download servers, used to size per-device bulk buffers


@dataclass
class FlowView:
    key: tuple  # (proto, lan_ip, lan_port, wan_ip, wan_port)
    proto: str
    lan_ip: str
    wan_ip: str
    first_ts: float
    last_ts: float
    bytes_up: int
    bytes_down: int
    vec: Optional[list] = None
    info: Optional[dict] = None


@dataclass
class ClassObs:
    sent_bytes: int = 0
    backlog_bytes: int = 0
    drops: int = 0


@dataclass
class Observation:
    now: float
    flows: list
    classes: dict  # dir -> {cid: ClassObs}
    capacity: dict  # dir -> shaper rate (bps)


@dataclass
class DirPlan:
    root_rate: float
    leaves: dict = field(default_factory=dict)  # cid -> dict(rate, ceil, prio, limit_bytes)
    inner: dict = field(default_factory=dict)


@dataclass
class Actions:
    assign: dict = field(default_factory=dict)
    plans: dict = field(default_factory=dict)
    decisions: list = field(default_factory=list)


class FlowState:
    __slots__ = ("key", "proto", "lan_ip", "wan_ip", "lan_port", "wan_port", "device", "first_seen",
                 "last_seen", "label", "conf", "probs", "tc", "cid", "hist", "last_classified",
                 "b_up", "b_down", "rate_up", "rate_down", "explanation", "pinned", "violations",
                 "demoted", "info", "n_pred", "rate_long")

    def __init__(self, fv: FlowView, device: str, now: float) -> None:
        self.key = fv.key
        self.proto = fv.proto
        self.lan_ip = fv.lan_ip
        self.wan_ip = fv.wan_ip
        self.lan_port = fv.key[2]
        self.wan_port = fv.key[4]
        self.device = device
        self.first_seen = fv.first_ts
        self.last_seen = now
        self.label: Optional[str] = None
        self.conf = 0.0
        self.probs: dict = {}
        self.tc: TC = DEFAULT_CLASS
        self.cid = int(DEFAULT_CLASS)
        self.hist: deque = deque(maxlen=3)
        self.last_classified = -1e9
        self.b_up = fv.bytes_up
        self.b_down = fv.bytes_down
        self.rate_up = 0.0
        self.rate_down = 0.0
        self.explanation = ""
        self.pinned: Optional[TC] = None
        self.violations = 0
        self.demoted = False
        self.info: dict = {}
        self.n_pred = 0
        self.rate_long = 0.0  # ~20 s EWMA of the downlink rate (a stream's average bitrate)

    def as_dict(self) -> dict:
        return {
            "key": list(self.key), "proto": self.proto, "device": self.device, "lan_ip": self.lan_ip,
            "lan_port": self.lan_port, "wan_ip": self.wan_ip, "wan_port": self.wan_port,
            "label": self.label, "label_name": APP_LABELS.get(self.label, "Classifying...") if self.label else "Classifying...",
            "confidence": round(self.conf, 3), "tc": int(self.tc), "class_name": CLASS_INFO[self.tc].name,
            "kbps_up": round(self.rate_up / 1000, 1), "kbps_down": round(self.rate_down / 1000, 1),
            "age_s": round(self.last_seen - self.first_seen, 1), "explanation": self.explanation,
            "demoted": self.demoted, "pinned": int(self.pinned) if self.pinned else None,
            "probs": {k: round(v, 3) for k, v in self.probs.items()},
        }


class _ClassTrack:
    def __init__(self) -> None:
        self.last_sent = 0
        self.served = 0.0
        self.arrival_hist: deque = deque(maxlen=10)  # 5 s of 500 ms samples
        self.sat_hist: deque = deque(maxlen=8)
        self.last_limit = 0
        self.last_rate = 0.0
        self.backlog = 0
        self.drops = 0
        self.last_drops = 0
        self.drop_rate = 0.0


class FlowPilotBrain:
    def __init__(self, devices: dict, classifier: Optional[FlowClassifier] = None,
                 intent: str = "balanced", headroom: float = 1.3) -> None:
        """devices: lan_ip -> device name (the router's DHCP table)."""
        self.devices = dict(devices)
        self.slots = {ip: i + 1 for i, ip in enumerate(sorted(self.devices))}
        self.clf = classifier or FlowClassifier()
        self.intent = intent
        self.weights = dict(INTENT_PRESETS[intent])
        self.headroom = headroom
        self.flows: dict[tuple, FlowState] = {}
        self.retired: deque = deque(maxlen=4096)  # (key, label, bytes) of expired labelled flows
        self.tracks = {d: {tc: _ClassTrack() for tc in TC} for d in DIRS}
        self.last_now: Optional[float] = None
        self.decisions: deque = deque(maxlen=400)
        self.last_alloc: dict = {}
        self.last_alloc_log: dict = {}
        self.class_status: dict = {d: {} for d in DIRS}
        self.device_pins: dict[str, TC] = {}
        self.stats = {"classified": 0, "reclassified": 0, "demoted": 0}
        self._pending: list = []
        self._vecs: dict = {}

    # ------------------------------------------------------------------ public controls
    def set_intent(self, intent: str, now: float) -> None:
        if intent not in INTENT_PRESETS:
            raise KeyError(intent)
        self.intent = intent
        self.weights = dict(INTENT_PRESETS[intent])
        self._log(now, "policy", f"Household intent set to '{intent.replace('_', ' ')}'",
                  {"weights": {CLASS_INFO[k].name: v for k, v in self.weights.items()}})

    def pin_device(self, device: str, tc: Optional[TC], now: float) -> None:
        if tc is None:
            self.device_pins.pop(device, None)
            self._log(now, "policy", f"Removed manual priority for {device}", {})
        else:
            self.device_pins[device] = TC(tc)
            self._log(now, "policy", f"{device} pinned to {CLASS_INFO[TC(tc)].name} by the user", {})

    def _log(self, now: float, kind: str, msg: str, details: dict) -> dict:
        d = {"t": round(now, 2), "kind": kind, "message": msg, "details": details}
        self.decisions.append(d)
        self._pending.append(d)
        return d

    def slot_cid(self, lan_ip: str) -> int:
        return BULK_INNER + min(9, self.slots.get(lan_ip, 9))

    def leaf_cid(self, f: FlowState) -> int:
        return self.slot_cid(f.lan_ip) if f.tc == TC.BULK else int(f.tc)

    # ------------------------------------------------------------------ main step
    def step(self, obs: Observation) -> Actions:
        self._pending: list = []
        now = obs.now
        dt = (now - self.last_now) if self.last_now is not None else 0.5
        dt = max(dt, 1e-3)
        self.last_now = now
        acts = Actions()
        self._ingest_flows(obs, dt, now)
        self._classify(now, acts)
        self._protect(now, acts)
        demands = self._estimate(obs, dt)
        for d in DIRS:
            acts.plans[d] = self._plan(d, obs.capacity[d], demands[d], now)
        self._expire(now)
        acts.decisions = self._pending
        return acts

    def _ingest_flows(self, obs: Observation, dt: float, now: float) -> None:
        for fv in obs.flows:
            f = self.flows.get(fv.key)
            if f is None:
                dev = self.devices.get(fv.lan_ip, fv.lan_ip)
                f = self.flows[fv.key] = FlowState(fv, dev, now)
                pin = self.device_pins.get(dev)
                if pin is not None:
                    f.tc = pin
                    f.pinned = pin
                else:
                    self._inherit(f, now)
            up = max(0, fv.bytes_up - f.b_up) * 8 / dt
            dn = max(0, fv.bytes_down - f.b_down) * 8 / dt
            f.b_up, f.b_down = fv.bytes_up, fv.bytes_down
            a = 0.5
            f.rate_up += a * (up - f.rate_up)
            f.rate_down += a * (dn - f.rate_down)
            f.rate_long += min(1.0, dt / 20.0) * (dn - f.rate_long)
            f.last_seen = fv.last_ts
            if fv.info is not None:
                f.info = fv.info
        self._vecs = {fv.key: fv.vec for fv in obs.flows if fv.vec is not None}

    def _inherit(self, f: FlowState, now: float) -> None:
        """A new connection from a device to a server it is already talking to most likely
        belongs to the same application (download managers, browsers and players open
        sibling connections): start it in the siblings' class instead of the default one."""
        votes: dict = {}
        for g in self.flows.values():
            if g is f or g.label is None or g.lan_ip != f.lan_ip or g.wan_ip != f.wan_ip or g.proto != f.proto:
                continue
            if now - g.last_seen < 10.0:
                votes[g.label] = votes.get(g.label, 0) + 1
        if votes:
            lab = max(votes, key=votes.get)
            f.label = lab
            f.conf = 0.5
            f.tc = APP_TO_CLASS[lab]
            f.explanation = f"inherited from {votes[lab]} sibling connection(s) to {f.wan_ip}"
            f.cid = -1  # force an assignment on the next classification pass

    def _classify(self, now: float, acts: Actions) -> None:
        due = [f for k, f in self.flows.items() if k in self._vecs and now - f.last_classified >= 0.95]
        if not due:
            return
        preds = self.clf.predict([self._vecs[f.key] for f in due])
        for f, (lab, conf, probs) in zip(due, preds):
            f.last_classified = now
            f.hist.append(lab)
            f.n_pred += 1
            f.probs = probs
            if f.pinned is not None:
                continue
            changed = False
            if f.label is None:
                if conf >= 0.55:
                    f.label, f.conf, changed = lab, conf, True
                    self.stats["classified"] += 1
            elif lab != f.label:
                if list(f.hist)[-2:] == [lab, lab] and conf >= 0.6:
                    old = f.label
                    f.label, f.conf, changed = lab, conf, True
                    self.stats["reclassified"] += 1
                    f.explanation = f"re-classified from {APP_LABELS.get(old, old)}: " + self.clf.explain(lab, f.info)
            else:
                f.conf = 0.7 * f.conf + 0.3 * conf
            if changed:
                if not f.explanation.startswith("re-classified"):
                    f.explanation = self.clf.explain(f.label, f.info)
                new_tc = APP_TO_CLASS[f.label] if not f.demoted else TC.BULK
                if new_tc != f.tc:
                    f.tc = new_tc
                    self._log(now, "classify",
                              f"{f.device}: {f.proto.upper()} flow to {f.wan_ip}:{f.wan_port} is "
                              f"{APP_LABELS[f.label]} ({f.conf * 100:.0f}%) -> {CLASS_INFO[f.tc].name}",
                              {"flow": list(f.key), "label": f.label, "confidence": f.conf, "tc": int(f.tc),
                               "why": f.explanation})
            cid = self.leaf_cid(f)
            if cid != f.cid:
                f.cid = cid
                acts.assign[f.key] = cid
        # flows whose bulk slot changed (pinning etc.)
        for f in self.flows.values():
            cid = self.leaf_cid(f)
            if cid != f.cid:
                f.cid = cid
                acts.assign[f.key] = cid

    def _protect(self, now: float, acts: Actions) -> None:
        budget = {TC.REALTIME: 1.5e6, TC.CONFERENCING: 12e6}
        for f in self.flows.values():
            b = budget.get(f.tc)
            if b is None or f.pinned is not None:
                f.violations = 0
                continue
            if max(f.rate_up, f.rate_down) > b:
                f.violations += 1
            else:
                f.violations = max(0, f.violations - 1)
            if f.violations >= 3:
                f.demoted = True
                old = f.tc
                f.tc = TC.BULK
                f.cid = self.leaf_cid(f)
                acts.assign[f.key] = f.cid
                self.stats["demoted"] += 1
                self._log(now, "protect",
                          f"{f.device}: flow to {f.wan_ip}:{f.wan_port} exceeded the {CLASS_INFO[old].name} budget "
                          f"({max(f.rate_up, f.rate_down) / 1e6:.1f} Mb/s) -> moved to Bulk (traffic protection)",
                          {"flow": list(f.key), "from": int(old)})

    def _estimate(self, obs: Observation, dt: float) -> dict:
        out = {}
        for d in DIRS:
            per = {tc: ClassDemand(tc) for tc in TC}
            burst_peak = {TC.REALTIME: 0.0, TC.CONFERENCING: 0.0}
            for f in self.flows.values():
                if obs.now - f.last_seen > 2.0:
                    continue
                r = f.rate_down if d == "down" else f.rate_up
                tc = f.tc
                if (d == "up" and f.proto == "tcp" and tc not in (TC.REALTIME, TC.CONFERENCING)
                        and f.info.get("mean_size_up", 1500) < 120):
                    tc = TC.INTERACTIVE  # pure-ACK uplink of a download is queued in the ACK class
                cd = per[tc]
                if r > 1_000 or tc in (TC.REALTIME, TC.CONFERENCING):
                    cd.nflows += 1 if (r > 1_000 or obs.now - f.last_seen < 1.0) else 0
                cd.arrival_bps += r
                if tc == TC.STREAMING and f.rate_down > 200_000:
                    cd.streams += 1
                if tc == TC.STREAMING and d == "down":
                    cd.stream_avg_bps += f.rate_long
                if tc in burst_peak and f.info:
                    pk = f.info.get("peak_kbps_down" if d == "down" else "peak_kbps_up", 0.0) * 1000
                    burst_peak[tc] += min(pk, 4 * max(r, 64_000))
            cls = obs.classes.get(d, {})
            for tc in TC:
                tr = self.tracks[d][tc]
                cd = per[tc]
                if tc == TC.BULK:
                    sent = sum(c.sent_bytes for cid, c in cls.items() if BULK_INNER < cid < BULK_INNER + 10)
                    backlog = sum(c.backlog_bytes for cid, c in cls.items() if BULK_INNER < cid < BULK_INNER + 10)
                    drops = sum(c.drops for cid, c in cls.items() if BULK_INNER < cid < BULK_INNER + 10)
                else:
                    c = cls.get(int(tc), ClassObs())
                    sent, backlog, drops = c.sent_bytes, c.backlog_bytes, c.drops
                served = max(0, sent - tr.last_sent) * 8 / dt if tr.last_sent else 0.0
                tr.last_sent = sent
                tr.served += 0.5 * (served - tr.served)
                tr.backlog = backlog
                tr.drop_rate = max(0, drops - tr.last_drops) / dt
                tr.last_drops = drops
                tr.arrival_hist.append(cd.arrival_bps)
                sat = (backlog > 0.3 * tr.last_limit and tr.last_limit > 0) or (
                    tr.last_rate > 0 and tr.served >= 0.85 * tr.last_rate) or tr.drop_rate > 0
                tr.sat_hist.append(sat)
                cd.peak_bps = max(tr.arrival_hist) if tr.arrival_hist else 0.0
                if tc in burst_peak:
                    # inelastic traffic is bursty at sub-second scale (video key-frames): size the
                    # guarantee on the 100 ms peak so bursts never wait behind elastic classes
                    cd.peak_bps = max(cd.peak_bps, 0.6 * burst_peak[tc])
                cd.served_bps = tr.served
                cd.backlog_bytes = backlog
                cd.saturated = any(tr.sat_hist) and cd.active
            out[d] = per
        return out

    def _plan(self, d: str, R: float, demands: dict, now: float) -> DirPlan:
        alloc = allocate(R, demands, self.weights, self.headroom)
        plan = DirPlan(root_rate=R)
        status = {}
        for tc, cp in alloc.classes.items():
            tr = self.tracks[d][tc]
            tr.last_limit = cp.limit_bytes
            tr.last_rate = cp.rate
            dem = demands[tc]
            status[int(tc)] = {
                "name": CLASS_INFO[tc].name, "rate": cp.rate, "ceil": cp.ceil, "prio": cp.prio,
                "limit_bytes": cp.limit_bytes, "demand": None if math.isinf(cp.demand) else cp.demand,
                "saturated": dem.saturated, "arrival": dem.arrival_bps, "served": tr.served,
                "backlog_bytes": tr.backlog, "flows": dem.nflows, "note": cp.note,
                "queue_delay_ms": (tr.backlog * 8 / max(tr.served, cp.rate, 1.0)) * 1000,
                "drop_rate": tr.drop_rate,
            }
            if tc == TC.BULK:
                plan.inner[BULK_INNER] = {"rate": cp.rate, "ceil": cp.ceil, "prio": cp.prio}
                nflows: dict = {}
                for f in self.flows.values():
                    if (f.tc == TC.BULK and now - f.last_seen < 2.0
                            and (f.rate_down if d == "down" else f.rate_up) > 1_000):
                        nflows[f.lan_ip] = nflows.get(f.lan_ip, 0) + 1
                n = max(1, len(nflows))
                served_share = max(cp.rate, tr.served) / n
                for ip in list(self.slots) + ["0.0.0.0"]:
                    cid = self.slot_cid(ip)
                    if cid in plan.leaves:
                        continue
                    share = cp.rate / n if ip in nflows else 8_000.0
                    # a device's TCP flows need ~ BDP / sqrt(#flows) of buffer to keep their share busy
                    # (Appenzeller et al., SIGCOMM 2004); never below the class delay budget
                    k = nflows.get(ip, 1)
                    t_buf = max(CLASS_INFO[tc].target_delay_ms / 1000, BULK_RTT_EST / math.sqrt(k))
                    limit = max(CLASS_INFO[tc].min_buffer_pkts * 1514, int(served_share / 8 * t_buf))
                    plan.leaves[cid] = {"rate": max(8_000.0, share), "ceil": cp.ceil, "prio": cp.prio,
                                        "limit_bytes": limit}
            else:
                leaf = {"rate": cp.rate, "ceil": cp.ceil, "prio": cp.prio, "limit_bytes": cp.limit_bytes}
                if tc in (TC.REALTIME, TC.CONFERENCING):
                    # token-bucket depth: let a whole burst (e.g. a key-frame) leave at line rate
                    leaf["burst"] = max(6_000.0, cp.rate / 8 * (0.040 if tc == TC.CONFERENCING else 0.020))
                plan.leaves[int(tc)] = leaf
        self.class_status[d] = status
        self._maybe_log_alloc(d, R, alloc, demands, now)
        return plan

    def _maybe_log_alloc(self, d: str, R: float, alloc, demands: dict, now: float) -> None:
        cur = {int(k): v.rate for k, v in alloc.classes.items() if demands[k].active}
        prev = self.last_alloc.get(d, {})
        # log only material changes, so classify / autorate / protect decisions stay visible:
        # a class (de)activated, or a guarantee moved by > 25 % *and* > 5 % of the link
        new_set = set(cur) != set(prev)
        moved = any(abs(cur[k] - prev.get(k, 0)) > max(0.25 * prev.get(k, 0), 0.05 * R) for k in cur)
        since = now - self.last_alloc_log.get(d, -1e9)
        if not ((new_set and since >= 1.0) or (moved and since >= 10.0)):
            return
        self.last_alloc[d] = cur
        self.last_alloc_log[d] = now
        parts = ", ".join(f"{CLASS_INFO[TC(k)].name} {v / 1e6:.1f}" for k, v in sorted(cur.items()))
        extra = " (call/game demand exceeds the protection cap: scaled)" if alloc.inelastic_scaled else ""
        self._log(now, "allocate", f"{'Downlink' if d == 'down' else 'Uplink'} {R / 1e6:.1f} Mb/s guaranteed: "
                  f"{parts} Mb/s{extra}", {"dir": d, "capacity": R, "alloc": cur})

    def _expire(self, now: float) -> None:
        dead = [k for k, f in self.flows.items() if now - f.last_seen > 15.0]
        for k in dead:
            f = self.flows.pop(k)
            if f.label is not None:
                self.retired.append((k, f.label, f.b_up + f.b_down))

    def labelled_flows(self):
        """(key, label, bytes) of every flow that got a label, including flows that have ended."""
        for k, f in self.flows.items():
            if f.label is not None:
                yield k, f.label, f.b_up + f.b_down
        yield from self.retired

    # ------------------------------------------------------------------ views
    def flow_table(self, now: float, limit: int = 200) -> list[dict]:
        rows = [f.as_dict() for f in self.flows.values() if now - f.last_seen < 5.0]
        rows.sort(key=lambda r: -(r["kbps_down"] + r["kbps_up"]))
        return rows[:limit]
