"""Program the home router's Linux data plane: tc (HTB/TBF/bfifo/fq_codel/cake) + nftables.

FlowPilot's data plane deliberately uses only primitives available on every Linux router
kernel, including embedded CPE vendor kernels without CAKE:

  * HTB tree per direction (egress of r-lan = downlink, egress of r-wan = uplink)
      1:1  root, rate = ceil = autorate capacity
      1:10 Real-time   1:20 Conferencing   1:30 Interactive (default)   1:40 Streaming
      1:50 Bulk (inner) -> 1:51 .. 1:59 one leaf per home device (per-device fairness)
  * leaf queues: fq_codel when the kernel has it, otherwise time-bounded bfifo whose byte
    limit FlowPilot resizes every 500 ms (rate x delay budget)
  * classification: the controller writes the class into the flow's conntrack mark
    (`conntrack -U ... --mark <cid>`); one nftables rule per class copies the mark into
    skb->priority, which HTB uses directly as the class id, and sets the DSCP so that
    Wi-Fi WMM (RFC 8325) and NQB-aware networks (RFC 9956) can honour it downstream
  * fixed rules: DNS and latency probes -> Real-time, pure TCP ACKs -> Interactive
"""
from __future__ import annotations

import json
from typing import Optional

from ..common.classes import CLASS_INFO, TC
from ..controller.brain import BULK_INNER, ClassObs
from ..sim.baselines import STATIC_HIGH_UDP
from .netns import CmdError, nsx, qdisc_supported, sh
from .topology import DEV_DOWN, DEV_UP, NS_ROUTER, ROUTER_WAN_IP

DIR_DEV = {"down": DEV_DOWN, "up": DEV_UP}


def mbit(bps: float) -> str:
    return f"{max(8_000.0, bps) / 1e6:.4f}mbit"


class KernelDataplane:
    def __init__(self, ns: str = NS_ROUTER, leaf: str = "auto") -> None:
        self.ns = ns
        if leaf == "auto":
            leaf = "fq_codel" if qdisc_supported("fq_codel") else "bfifo"
        self.leaf = leaf
        self.mode = "none"
        self.marks: dict[tuple, int] = {}
        self.errors = 0
        self.last_limits: dict = {}

    # ------------------------------------------------------------------ low level
    def tc_batch(self, cmds: list[str]) -> None:
        if not cmds:
            return
        try:
            sh(f"tc -n {self.ns} -force -batch -", input="\n".join(cmds) + "\n")
        except CmdError:
            self.errors += 1
            # fall back to one by one so a single bad line does not drop the whole plan
            for c in cmds:
                sh(f"tc -n {self.ns} {c}", check=False)

    def nft(self, ruleset: str) -> None:
        nsx(self.ns, "nft -f -", input=ruleset)

    def reset(self) -> None:
        for dev in DIR_DEV.values():
            sh(f"tc -n {self.ns} qdisc del dev {dev} root", check=False)
        nsx(self.ns, "nft delete table inet fp", check=False)
        nsx(self.ns, "conntrack -F", check=False)
        self.marks.clear()
        self.mode = "none"

    # ------------------------------------------------------------------ schedulers
    def setup(self, scheduler: str, rate_down: float, rate_up: float) -> None:
        self.reset()
        if scheduler == "fifo":
            for dev in DIR_DEV.values():
                sh(f"tc -n {self.ns} qdisc replace dev {dev} root pfifo limit 1000")
        elif scheduler == "static_qos":
            self._static(rate_down, rate_up)
        elif scheduler in ("flowpilot", "flowpilot_rl", "flowpilot_fq"):
            self._flowpilot(rate_down, rate_up)
        elif scheduler in ("fq_codel", "cake", "cake_autorate"):
            self._sqm("cake" if scheduler.startswith("cake") else "fq_codel", rate_down, rate_up)
        else:
            raise KeyError(scheduler)
        self.mode = scheduler

    def _sqm(self, kind: str, rd: float, ru: float) -> None:
        if not qdisc_supported(kind):
            raise RuntimeError(f"this kernel has no {kind} qdisc")
        for d, dev in DIR_DEV.items():
            r = rd if d == "down" else ru
            if kind == "cake":
                extra = " ingress" if d == "down" else ""
                sh(f"tc -n {self.ns} qdisc replace dev {dev} root cake bandwidth {mbit(r)} besteffort "
                   f"triple-isolate{extra}")
            else:
                self.tc_batch([f"qdisc replace dev {dev} root handle 1: htb default 10",
                               f"class add dev {dev} parent 1: classid 1:10 htb rate {mbit(r)} ceil {mbit(r)}",
                               f"qdisc add dev {dev} parent 1:10 handle 10: fq_codel"])

    def set_sqm_rate(self, d: str, rate: float) -> None:
        dev = DIR_DEV[d]
        if self.mode.startswith("cake"):
            sh(f"tc -n {self.ns} qdisc change dev {dev} root cake bandwidth {mbit(rate)}", check=False)
        else:
            sh(f"tc -n {self.ns} class change dev {dev} parent 1: classid 1:10 htb rate {mbit(rate)} "
               f"ceil {mbit(rate)}", check=False)

    def _static(self, rd: float, ru: float) -> None:
        cmds = []
        for d, dev in DIR_DEV.items():
            r = rd if d == "down" else ru
            cmds += [f"qdisc replace dev {dev} root handle 1: htb default 30 r2q 10",
                     f"class add dev {dev} parent 1: classid 1:1 htb rate {mbit(r)} ceil {mbit(r)}",
                     f"class add dev {dev} parent 1:1 classid 1:10 htb rate {mbit(0.2 * r)} ceil {mbit(r)} prio 0",
                     f"class add dev {dev} parent 1:1 classid 1:30 htb rate {mbit(0.7 * r)} ceil {mbit(r)} prio 1",
                     f"class add dev {dev} parent 1:1 classid 1:50 htb rate {mbit(0.1 * r)} ceil {mbit(r)} prio 2",
                     f"qdisc add dev {dev} parent 1:10 handle 10: pfifo limit 100",
                     f"qdisc add dev {dev} parent 1:30 handle 30: pfifo limit 300",
                     f"qdisc add dev {dev} parent 1:50 handle 50: pfifo limit 300"]
        self.tc_batch(cmds)
        ports = ", ".join(f"{lo}-{hi}" if lo != hi else str(lo) for lo, hi in STATIC_HIGH_UDP)
        self.nft(f"""
table inet fp {{
  chain post {{
    type filter hook postrouting priority mangle; policy accept;
    ip saddr {ROUTER_WAN_IP} udp dport 7 meta priority set 1:10 accept
    udp dport {{ {ports} }} meta priority set 1:10 accept
    udp sport {{ {ports} }} meta priority set 1:10 accept
    oifname "{DEV_UP}" meta l4proto tcp tcp flags & (fin|syn|rst|ack) == ack meta length < 120 meta priority set 1:10 accept
  }}
}}""")

    def _flowpilot(self, rd: float, ru: float) -> None:
        cmds = []
        for d, dev in DIR_DEV.items():
            r = rd if d == "down" else ru
            cmds += [f"qdisc replace dev {dev} root handle 1: htb default 30 r2q 10",
                     f"class add dev {dev} parent 1: classid 1:1 htb rate {mbit(r)} ceil {mbit(r)}"]
            for tc in (TC.REALTIME, TC.CONFERENCING, TC.INTERACTIVE, TC.STREAMING):
                info = CLASS_INFO[tc]
                cmds.append(f"class add dev {dev} parent 1:1 classid 1:{int(tc)} htb rate {mbit(0.05 * r)} "
                            f"ceil {mbit(r)} prio {info.htb_prio}")
                cmds.append(self._leaf_cmd("add", dev, int(tc), 64 * 1514))
            cmds.append(f"class add dev {dev} parent 1:1 classid 1:{BULK_INNER} htb rate {mbit(0.05 * r)} "
                        f"ceil {mbit(r)} prio {CLASS_INFO[TC.BULK].htb_prio}")
            for slot in range(1, 10):
                cid = BULK_INNER + slot
                cmds.append(f"class add dev {dev} parent 1:{BULK_INNER} classid 1:{cid} htb rate 8kbit "
                            f"ceil {mbit(r)} prio {CLASS_INFO[TC.BULK].htb_prio}")
                cmds.append(self._leaf_cmd("add", dev, cid, 32 * 1514))
        self.tc_batch(cmds)
        rules = [
            f"ip saddr {ROUTER_WAN_IP} udp dport 7 meta priority set 1:10 accept",
            "udp dport 53 meta priority set 1:10 accept",
            "udp sport 53 meta priority set 1:10 accept",
            "meta l4proto tcp tcp flags & (fin|syn|rst|ack) == ack meta length < 120 meta priority set 1:30 accept",
        ]
        dscp = {TC.REALTIME: "45", TC.CONFERENCING: "af41", TC.INTERACTIVE: "cs0", TC.STREAMING: "af31"}
        for tc in (TC.REALTIME, TC.CONFERENCING, TC.INTERACTIVE, TC.STREAMING):
            rules.append(f"ct mark {int(tc)} meta priority set 1:{int(tc)} ip dscp set {dscp[tc]} accept")
        for slot in range(1, 10):
            cid = BULK_INNER + slot
            rules.append(f"ct mark {cid} meta priority set 1:{cid} ip dscp set 1 accept")  # RFC 8622 LE
        body = "\n    ".join(rules)
        self.nft(f"""
table inet fp {{
  chain post {{
    type filter hook postrouting priority mangle; policy accept;
    {body}
  }}
}}""")

    def _leaf_cmd(self, verb: str, dev: str, cid: int, limit: int) -> str:
        if self.leaf == "fq_codel":
            return f"qdisc {verb} dev {dev} parent 1:{cid} handle {cid}: fq_codel"
        return f"qdisc {verb} dev {dev} parent 1:{cid} handle {cid}: bfifo limit {int(limit)}"

    # ------------------------------------------------------------------ FlowPilot actions
    def set_root(self, d: str, rate: float) -> None:
        dev = DIR_DEV[d]
        self.tc_batch([f"class change dev {dev} parent 1: classid 1:1 htb rate {mbit(rate)} ceil {mbit(rate)}"])

    def apply_plan(self, d: str, plan) -> None:
        dev = DIR_DEV[d]
        cmds = [f"class change dev {dev} parent 1: classid 1:1 htb rate {mbit(plan.root_rate)} "
                f"ceil {mbit(plan.root_rate)}"]
        for cid, p in plan.inner.items():
            cmds.append(f"class change dev {dev} parent 1:1 classid 1:{cid} htb rate {mbit(p['rate'])} "
                        f"ceil {mbit(p['ceil'])} prio {p['prio']}")
        for cid, p in plan.leaves.items():
            parent = f"1:{BULK_INNER}" if BULK_INNER < cid < BULK_INNER + 10 else "1:1"
            burst = f" burst {int(p['burst'])}" if p.get("burst") else ""
            cmds.append(f"class change dev {dev} parent {parent} classid 1:{cid} htb rate {mbit(p['rate'])} "
                        f"ceil {mbit(p['ceil'])} prio {p['prio']}{burst}")
            if self.leaf == "bfifo":
                lim = int(p["limit_bytes"])
                key = (d, cid)
                old = self.last_limits.get(key)
                if old is None or abs(lim - old) > 0.1 * old:
                    self.last_limits[key] = lim
                    cmds.append(self._leaf_cmd("change", dev, cid, lim))
        self.tc_batch(cmds)

    def assign(self, key: tuple, cid: int) -> bool:
        """Write the class into the flow's conntrack entry (both directions follow)."""
        proto, lan_ip, lan_port, wan_ip, wan_port = key
        base = (f"conntrack -U -p {proto} --orig-src {lan_ip} --orig-dst {wan_ip} "
                f"--orig-port-src {lan_port} --orig-port-dst {wan_port} --mark {cid}")
        try:
            nsx(self.ns, base)
            self.marks[key] = cid
            return True
        except CmdError:
            # flow initiated from the WAN side: match the reply tuple instead
            try:
                nsx(self.ns, f"conntrack -U -p {proto} --reply-src {lan_ip} --reply-dst {wan_ip} "
                             f"--reply-port-src {lan_port} --reply-port-dst {wan_port} --mark {cid}")
                self.marks[key] = cid
                return True
            except CmdError:
                self.errors += 1
                return False

    # ------------------------------------------------------------------ telemetry
    def class_stats(self, d: str) -> dict:
        dev = DIR_DEV[d]
        out: dict[int, ClassObs] = {}
        try:
            classes = json.loads(sh(f"tc -n {self.ns} -s -j class show dev {dev}") or "[]")
            qdiscs = json.loads(sh(f"tc -n {self.ns} -s -j qdisc show dev {dev}") or "[]")
        except (CmdError, json.JSONDecodeError):
            return out
        leaf_q = {}
        for q in qdiscs:
            parent = q.get("parent")
            if parent and parent.startswith("1:"):
                leaf_q[int(parent.split(":")[1], 16)] = q
        for c in classes:
            h = c.get("handle", "")
            if not h.startswith("1:"):
                continue
            cid = int(h.split(":")[1], 16)
            st = c.get("stats", c)
            q = leaf_q.get(cid, {})
            out[cid] = ClassObs(sent_bytes=int(st.get("bytes", 0)),
                                backlog_bytes=int(q.get("backlog", st.get("backlog", 0))),
                                drops=int(q.get("drops", st.get("drops", 0))))
        return out

    def tx_bytes(self, d: str) -> int:
        dev = DIR_DEV[d]
        try:
            data = json.loads(sh(f"ip -n {self.ns} -s -j link show dev {dev}"))
            return int(data[0]["stats64"]["tx"]["bytes"])
        except (CmdError, KeyError, IndexError, json.JSONDecodeError):
            return 0

    def qdisc_summary(self) -> dict:
        out = {}
        for d, dev in DIR_DEV.items():
            try:
                out[d] = json.loads(sh(f"tc -n {self.ns} -s -j qdisc show dev {dev}") or "[]")
            except (CmdError, json.JSONDecodeError):
                out[d] = []
        return out
