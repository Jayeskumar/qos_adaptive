"""Packets, flows and store-and-forward links.

A direction of the home network (downlink or uplink) is a chain of `Link`s. Each link
serialises packets at `rate_bps`, holds them in a queueing discipline (`qdisc`) while
busy, and hands them to `next_hop` after the propagation delay.
"""
from __future__ import annotations

from typing import Callable, Optional

from .engine import Sim

MTU = 1500
TCP_HDR = 52  # IPv4 + TCP with timestamps
UDP_HDR = 28  # IPv4 + UDP

# packet kinds
K_UDP = 0
K_TCP_DATA = 1
K_TCP_ACK = 2
K_CTRL = 3  # SYN / HTTP request: small, retransmitted on loss


class Flow:
    """A transport flow (5-tuple). `lan_ip` is always the home device side."""

    __slots__ = (
        "fid", "proto", "lan_ip", "lan_port", "wan_ip", "wan_port", "device", "app_type",
        "app_id", "tc", "hash", "prop_delay", "recv_down", "recv_up", "dscp", "start", "key",
    )

    def __init__(self, fid: int, proto: str, lan_ip: str, lan_port: int, wan_ip: str, wan_port: int,
                 device: str, app_type: str, app_id: str, prop_delay: float, start: float = 0.0) -> None:
        self.fid = fid
        self.proto = proto
        self.lan_ip = lan_ip
        self.lan_port = lan_port
        self.wan_ip = wan_ip
        self.wan_port = wan_port
        self.device = device
        self.app_type = app_type  # ground truth (never shown to the controller)
        self.app_id = app_id
        self.tc = 0  # traffic class assigned by the controller (0 = unassigned)
        self.hash = hash((proto, lan_ip, lan_port, wan_ip, wan_port)) & 0x7FFFFFFF
        self.prop_delay = prop_delay  # one-way Internet path delay beyond the ISP edge
        self.recv_down: Optional[Callable[["Packet"], None]] = None
        self.recv_up: Optional[Callable[["Packet"], None]] = None
        self.dscp = 0
        self.start = start
        self.key = (proto, lan_ip, lan_port, wan_ip, wan_port)

    def rekey(self) -> None:
        self.key = (self.proto, self.lan_ip, self.lan_port, self.wan_ip, self.wan_port)


class Packet:
    __slots__ = ("flow", "size", "kind", "seq", "ack", "sack", "ts", "enq_ts", "up", "on_drop", "payload")

    def __init__(self, flow: Flow, size: int, kind: int, up: bool, ts: float,
                 seq: int = 0, ack: int = 0, sack: int = -1, payload: object = None) -> None:
        self.flow = flow
        self.size = size
        self.kind = kind
        self.up = up
        self.ts = ts  # sender timestamp (echoed for RTT / used for one-way delay)
        self.seq = seq
        self.ack = ack
        self.sack = sack
        self.enq_ts = 0.0
        self.on_drop: Optional[Callable[["Packet"], None]] = None
        self.payload = payload


class Link:
    """Store-and-forward link with a pluggable qdisc (work-conserving or shaped).

    Qdiscs report every drop (tail drop, AQM drop, drop-from-fattest-flow) through the
    `drop_hook` the link installs on them, so loss accounting is uniform.
    """

    __slots__ = ("sim", "name", "rate", "prop", "qdisc", "next_hop", "busy", "_wake_at",
                 "tx_bytes", "drops", "on_ingress", "_wake_gen")

    def __init__(self, sim: Sim, name: str, rate_bps: float, prop_delay: float, qdisc,
                 next_hop: Optional[Callable[[Packet], None]] = None) -> None:
        self.sim = sim
        self.name = name
        self.rate = float(rate_bps)
        self.prop = prop_delay
        self.qdisc = qdisc
        qdisc.drop_hook = self._dropped
        self.next_hop = next_hop
        self.busy = False
        self._wake_at = -1.0
        self._wake_gen = 0
        self.tx_bytes = 0
        self.drops = 0
        self.on_ingress: Optional[Callable[[Packet, float], None]] = None

    def set_qdisc(self, qdisc) -> None:
        """Hot-swap the queueing discipline, migrating any queued packets."""
        old = self.qdisc
        self.qdisc = qdisc
        qdisc.drop_hook = self._dropped
        now = self.sim.now
        for p in old.drain():
            qdisc.enqueue(p, now)
        self._kick()

    def set_rate(self, rate_bps: float) -> None:
        self.rate = float(rate_bps)

    def arrive(self, pkt: Packet) -> None:
        now = self.sim.now
        if self.on_ingress is not None:
            self.on_ingress(pkt, now)
        pkt.enq_ts = now
        self.qdisc.enqueue(pkt, now)
        if not self.busy:
            self._try_send()

    def _dropped(self, pkt: Packet) -> None:
        self.drops += 1
        if pkt.on_drop is not None:
            pkt.on_drop(pkt)

    def _kick(self) -> None:
        if not self.busy:
            self._try_send()

    def _try_send(self) -> None:
        pkt, wake = self.qdisc.dequeue(self.sim.now)
        if pkt is not None:
            self.busy = True
            self.sim.after(pkt.size * 8.0 / self.rate, self._tx_done, pkt)
        elif wake is not None:
            # shaped qdisc: nothing eligible right now, come back when tokens allow
            if self._wake_at < 0 or wake < self._wake_at - 1e-9 or self._wake_at <= self.sim.now:
                self._wake_gen += 1
                self._wake_at = wake
                self.sim.at(wake, self._wake, self._wake_gen)

    def _wake(self, gen: int) -> None:
        if gen != self._wake_gen:
            return
        self._wake_at = -1.0
        if not self.busy:
            self._try_send()

    def _tx_done(self, pkt: Packet) -> None:
        self.busy = False
        self.tx_bytes += pkt.size
        if self.next_hop is not None:
            if self.prop > 0:
                self.sim.after(self.prop, self.next_hop, pkt)
            else:
                self.next_hop(pkt)
        self._try_send()
