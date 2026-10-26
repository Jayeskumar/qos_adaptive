"""The FlowPilot controller running against the real Linux kernel of the testbed router.

Three loops, all in the main process's asyncio loop (data-plane writes go through one
worker thread so tc/nft/conntrack calls never block the event loop):

  probe      20 Hz UDP probes from the router's WAN address to a public-side reflector;
             one-way delays in both directions (shared monotonic clock)
  autorate   every 200 ms: probe deltas + shaper load -> HTB root rate per direction
  control    every 500 ms: sniffer flows + tc class counters -> FlowPilot brain ->
             conntrack marks (flow -> class) and HTB plan (rates, ceils, buffers)
"""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ..controller.autorate import DelayAutorate
from ..controller.brain import FlowPilotBrain, FlowView, Observation
from ..controller.classifier import FlowClassifier
from . import sniffer
from .dataplane import KernelDataplane
from .apps import UdpEndpoint
from .netns import ns_socket
from .topology import NS_INET, NS_ROUTER, ROUTER_LAN_IP, ROUTER_WAN_IP, DEV_DOWN, DEV_UP, Topology

PROBE = struct.Struct("!HIqq")
REFLECTOR_PORT = 7


class Prober:
    """20 Hz router -> reflector probes; both one-way delays from kernel timestamps."""

    def __init__(self, reflector_ip: str, hz: float = 20.0) -> None:
        self.reflector_ip = reflector_ip
        self.hz = hz
        self.samples: list = []
        self.seq = 0
        self.sent = self.recv = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        rsock = ns_socket(NS_INET, bind=(self.reflector_ip, REFLECTOR_PORT))
        psock = ns_socket(NS_ROUTER, bind=(ROUTER_WAN_IP, 0))
        psock.connect((self.reflector_ip, REFLECTOR_PORT))
        self.refl = UdpEndpoint(rsock, self._reflect)
        self.prob = UdpEndpoint(psock, self._back)
        self.socks = (rsock, psock)
        self.task = loop.create_task(self._tick())

    async def _tick(self) -> None:
        nxt = time.monotonic()
        while True:
            self.prob.sendto(PROBE.pack(0xF1F1, self.seq, time.time_ns(), 0))
            self.sent += 1
            self.seq += 1
            nxt += 1.0 / self.hz
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    def _reflect(self, data: bytes, addr, rx_ns: int) -> None:
        _, seq, ts, _ = PROBE.unpack_from(data)
        # the kernel arrival time at the reflector closes the uplink measurement
        self.refl.sendto(PROBE.pack(0xF1F1, seq, ts, rx_ns), addr)
        self._last_reflect_tx = time.time_ns()

    def _back(self, data: bytes, addr, rx_ns: int) -> None:
        _, seq, ts, tr = PROBE.unpack_from(data)
        self.recv += 1
        # downlink leg: from the reflector's kernel arrival + its (tiny) turnaround, the
        # turnaround being measured on the same clock is negligible next to queueing delay
        self.samples.append((rx_ns / 1e9, (tr - ts) / 1e6, (rx_ns - tr) / 1e6))

    def drain(self) -> list:
        out, self.samples = self.samples, []
        return out

    def stop(self) -> None:
        self.task.cancel()
        self.refl.close()
        self.prob.close()
        for s in self.socks:
            s.close()


class KernelController:
    """FlowPilot (or a delay-autorate baseline) driving the testbed router's kernel."""

    def __init__(self, topo: Topology, dp: KernelDataplane, scheduler: str = "flowpilot",
                 classifier: Optional[FlowClassifier] = None, intent: str = "balanced",
                 autorate=None, rate_factor: float = 0.95) -> None:
        self.topo = topo
        self.dp = dp
        self.scheduler = scheduler
        self.is_fp = scheduler.startswith("flowpilot")
        self.autorate = autorate or DelayAutorate(max_frac=0.95)
        self.rate_factor = rate_factor
        devices = {d.ip: d.name for d in topo.devices}
        self.brain = FlowPilotBrain(devices, classifier or FlowClassifier(), intent=intent) if self.is_fp else None
        self.exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dataplane")
        self.prober = Prober(topo.server_ip("reflector"))
        self.flows_msg: Optional[dict] = None
        self.decisions: list = []
        self.rates = {"down": topo.down_mbps * 1e6 * rate_factor, "up": topo.up_mbps * 1e6 * rate_factor}
        self.applied_rates = dict(self.rates)
        self.tasks: list = []
        self.sniffer_pkts = 0

    async def _dp(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.exec, fn, *args)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.autorate.reset(self.rates["down"], self.rates["up"], self.topo.down_mbps * 1e6, self.topo.up_mbps * 1e6)
        await self.prober.start()
        if self.is_fp:
            ctx = mp.get_context("spawn")
            self.sn_parent, child = ctx.Pipe()
            self.sn_stop = ctx.Event()
            cfg = {"ns": NS_ROUTER, "wan_dev": DEV_UP, "lan_dev": DEV_DOWN, "lan_net": "192.168.1.0/24",
                   "ignore": [ROUTER_LAN_IP, ROUTER_WAN_IP]}
            self.sn_proc = ctx.Process(target=sniffer.main, args=(child, cfg, self.sn_stop), daemon=True)
            self.sn_proc.start()
            loop.add_reader(self.sn_parent.fileno(), self._sniffer_readable)
            self.tasks.append(loop.create_task(self._control_loop()))
        self.tasks.append(loop.create_task(self._autorate_loop()))

    def _sniffer_readable(self) -> None:
        try:
            while self.sn_parent.poll():
                msg = self.sn_parent.recv()
                if msg.get("type") == "flows":
                    self.flows_msg = msg
                    self.sniffer_pkts = msg.get("pkts", 0)
        except (EOFError, OSError):
            asyncio.get_running_loop().remove_reader(self.sn_parent.fileno())

    async def _autorate_loop(self) -> None:
        last = time.monotonic()
        tx0 = {d: await self._dp(self.dp.tx_bytes, d) for d in ("down", "up")}
        self.autorate.dirs["down"].last_tx = tx0["down"]
        self.autorate.dirs["up"].last_tx = tx0["up"]
        while True:
            await asyncio.sleep(self.autorate.interval)
            now = time.monotonic()
            dt = now - last
            last = now
            tx = {d: await self._dp(self.dp.tx_bytes, d) for d in ("down", "up")}
            rd, ru = self.autorate.update(now, self.prober.drain(), tx["down"], tx["up"], dt)
            self.rates = {"down": rd, "up": ru}
            for ev in self.autorate.drain_events():
                self._decision(now, "autorate",
                               f"Bufferbloat on the {'downlink' if ev['dir'] == 'down' else 'uplink'} "
                               f"(+{ev['delta_ms']:.0f} ms): ISP capacity fell, shaper "
                               f"{ev['from'] / 1e6:.1f} -> {ev['to'] / 1e6:.1f} Mb/s", ev)
            for d in ("down", "up"):
                if abs(self.rates[d] - self.applied_rates[d]) > 0.01 * self.applied_rates[d]:
                    self.applied_rates[d] = self.rates[d]
                    if self.is_fp:
                        await self._dp(self.dp.set_root, d, self.rates[d])
                    else:
                        await self._dp(self.dp.set_sqm_rate, d, self.rates[d])

    def _decision(self, now: float, kind: str, msg: str, details: dict) -> None:
        d = {"t": round(now - self.t0, 2) if hasattr(self, "t0") else 0.0, "kind": kind, "message": msg,
             "details": details}
        self.decisions.append(d)
        if self.brain is not None:
            self.brain.decisions.append(d)

    async def _control_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            msg = self.flows_msg
            if msg is None:
                continue
            now = time.monotonic()
            flows = [FlowView(tuple(f["key"]), f["proto"], f["lan_ip"], f["wan_ip"], f["first_ts"], f["last_ts"],
                              f["bytes_up"], f["bytes_down"], f.get("vec"), f.get("info")) for f in msg["flows"]]
            classes = {d: await self._dp(self.dp.class_stats, d) for d in ("down", "up")}
            obs = Observation(now, flows, classes, dict(self.rates))
            acts = self.brain.step(obs)
            for d in acts.decisions:
                d["t"] = round(now - self.t0, 2) if hasattr(self, "t0") else d["t"]
                self.decisions.append(d)
            for key, cid in acts.assign.items():
                await self._dp(self.dp.assign, key, cid)
            for d, plan in acts.plans.items():
                plan.root_rate = self.rates[d]
                await self._dp(self.dp.apply_plan, d, plan)

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.prober.stop()
        if self.is_fp:
            asyncio.get_running_loop().remove_reader(self.sn_parent.fileno())
            self.sn_stop.set()
            self.sn_proc.join(2)
            if self.sn_proc.is_alive():
                self.sn_proc.terminate()
        self.exec.shutdown(wait=False)

    def status(self) -> dict:
        st = {"scheduler": self.scheduler, "autorate": self.autorate.status(), "probe_sent": self.prober.sent,
              "probe_recv": self.prober.recv, "dataplane_errors": self.dp.errors, "sniffer_pkts": self.sniffer_pkts}
        if self.brain is not None:
            st["classes"] = self.brain.class_status
        return st
