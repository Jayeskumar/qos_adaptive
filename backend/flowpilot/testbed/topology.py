"""Build and tear down the emulated home network with real Linux networking.

    fp-inet  (Internet: game/call/CDN/mirror/web/backup servers + latency reflector)
       | i-isp <-> x-inet          TBF uplink bottleneck (C_up, deep FIFO) on x-inet egress
    fp-isp   (ISP access network)
       | x-home <-> r-wan          TBF downlink bottleneck (C_down, deep FIFO) on x-home egress
    fp-router (home gateway: FlowPilot / baseline qdiscs on r-lan (down) and r-wan (up))
       | r-lan <-> l-up
    fp-lan   (Wi-Fi/Ethernet switch: bridge br0)
       | l-<dev> <-> eth0
    fp-dev-<name> x 6 (console, laptop, tv, pc, phone, tablet)

All veths have GSO/TSO/GRO disabled so packets on the wire are real 1500-byte frames, as
on a physical access link (otherwise 64 KB super-packets would distort shaping).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..sim.world import DEFAULT_DEVICES, DEFAULT_SERVERS
from .netns import exists, list_ns, nsx, sh

PREFIX = "fp-"
NS_INET = "fp-inet"
NS_ISP = "fp-isp"
NS_ROUTER = "fp-router"
NS_LAN = "fp-lan"
ROUTER_LAN_IP = "192.168.1.1"
ROUTER_WAN_IP = "100.64.0.2"
ISP_GW_IP = "100.64.0.1"
INET_GW_IP = "203.0.113.1"
DEV_DOWN = "r-lan"  # router egress towards the home (downlink shaping)
DEV_UP = "r-wan"  # router egress towards the ISP (uplink shaping)


def allow_cc(cc: str) -> None:
    path = "/proc/sys/net/ipv4/tcp_allowed_congestion_control"
    try:
        with open(path) as f:
            allowed = f.read().split()
        if cc not in allowed:
            with open(path, "w") as f:
                f.write(" ".join(allowed + [cc]))
    except OSError:
        pass


def dev_ns(name: str) -> str:
    return f"fp-dev-{name}"


@dataclass
class Topology:
    down_mbps: float = 50.0
    up_mbps: float = 10.0
    isp_buffer_ms_down: float = 250.0
    isp_buffer_ms_up: float = 500.0
    base_delay_ms: float = 0.0  # one-way netem delay Internet<->ISP when netem exists
    delay_mode: str = "tun"  # "tun": userspace delay line (per-server delays), "none": veth
    access_delay_ms: float = 2.0
    lan_delay_ms: float = 0.5
    devices: list = field(default_factory=lambda: list(DEFAULT_DEVICES))
    servers: list = field(default_factory=lambda: list(DEFAULT_SERVERS))

    # ------------------------------------------------------------------ helpers
    def isp_limit_bytes(self, up: bool) -> int:
        rate = (self.up_mbps if up else self.down_mbps) * 1e6
        ms = self.isp_buffer_ms_up if up else self.isp_buffer_ms_down
        return max(30_000, int(rate / 8 * ms / 1000))

    @staticmethod
    def _veth(ns_a: str, a: str, ns_b: str, b: str) -> None:
        nsx(ns_a, f"ip link add {a} type veth peer name {b} netns {ns_b}")
        for ns, d in ((ns_a, a), (ns_b, b)):
            nsx(ns, f"ethtool -K {d} tso off gso off gro off", check=False)
            nsx(ns, f"ip link set {d} up")

    # ------------------------------------------------------------------ lifecycle
    def build(self) -> None:
        self.teardown()
        for ns in (NS_INET, NS_ISP, NS_ROUTER, NS_LAN) + tuple(dev_ns(d.name) for d in self.devices):
            sh(f"ip netns add {ns}")
            nsx(ns, "ip link set lo up")
        # Internet <-> ISP: a delay line (two TUNs + relay process) or a plain veth
        if self.delay_mode == "tun":
            nsx(NS_ISP, "ip tuntap add dev x-inet mode tun")
            nsx(NS_INET, "ip tuntap add dev i-isp mode tun")
            nsx(NS_ISP, "ip link set x-inet up")
            nsx(NS_INET, "ip link set i-isp up")
        else:
            self._veth(NS_INET, "i-isp", NS_ISP, "x-inet")
        nsx(NS_INET, "ip addr add 203.0.113.2/24 dev i-isp")
        for s in self.servers:
            nsx(NS_INET, f"ip addr add {s.ip}/32 dev i-isp")
        nsx(NS_INET, f"ip route add default via {INET_GW_IP} dev i-isp")
        nsx(NS_ISP, f"ip addr add {INET_GW_IP}/24 dev x-inet")
        # ISP <-> router
        self._veth(NS_ISP, "x-home", NS_ROUTER, DEV_UP)
        nsx(NS_ISP, f"ip addr add {ISP_GW_IP}/30 dev x-home")
        nsx(NS_ISP, f"ip route add 192.168.1.0/24 via {ROUTER_WAN_IP}")
        nsx(NS_ISP, "sysctl -qw net.ipv4.ip_forward=1")
        nsx(NS_ROUTER, f"ip addr add {ROUTER_WAN_IP}/30 dev {DEV_UP}")
        nsx(NS_ROUTER, f"ip route add default via {ISP_GW_IP}")
        nsx(NS_ROUTER, "sysctl -qw net.ipv4.ip_forward=1")
        # router <-> LAN switch
        self._veth(NS_ROUTER, DEV_DOWN, NS_LAN, "l-up")
        nsx(NS_ROUTER, f"ip addr add {ROUTER_LAN_IP}/24 dev {DEV_DOWN}")
        nsx(NS_LAN, "ip link add br0 type bridge")
        nsx(NS_LAN, "ip link set l-up master br0")
        nsx(NS_LAN, "ip link set br0 up")
        for d in self.devices:
            ns = dev_ns(d.name)
            port = f"l-{d.name}"[:15]
            self._veth(NS_LAN, port, ns, "eth0")
            nsx(NS_LAN, f"ip link set {port} master br0")
            nsx(ns, f"ip addr add {d.ip}/24 dev eth0")
            nsx(ns, f"ip route add default via {ROUTER_LAN_IP}")
        # TCP stacks: Linux defaults (CUBIC), slow start after idle on, as on real devices.
        # A child netns may only select congestion controls listed as "allowed" globally.
        allow_cc("cubic")
        for ns in [NS_INET] + [dev_ns(d.name) for d in self.devices]:
            nsx(ns, "sysctl -qw net.ipv4.tcp_congestion_control=cubic", check=False)
        self.apply_isp()

    def apply_isp(self, down_mbps: float | None = None, up_mbps: float | None = None) -> None:
        """Program the ISP bottlenecks. Buffers stay fixed in bytes when capacity changes,
        like real hardware, so a capacity drop also deepens the buffer in time."""
        first = down_mbps is None and up_mbps is None
        if down_mbps is not None:
            self.cur_down = down_mbps
        if up_mbps is not None:
            self.cur_up = up_mbps
        if first:
            self.cur_down, self.cur_up = self.down_mbps, self.up_mbps
        verb = "replace"
        nsx(NS_ISP, f"tc qdisc {verb} dev x-home root handle 1: tbf rate {self.cur_down:.3f}mbit "
                    f"burst 32kb limit {self.isp_limit_bytes(False)}")
        nsx(NS_ISP, f"tc qdisc {verb} dev x-inet root handle 1: tbf rate {self.cur_up:.3f}mbit "
                    f"burst 16kb limit {self.isp_limit_bytes(True)}")
        if self.base_delay_ms > 0:
            nsx(NS_ISP, f"tc qdisc {verb} dev x-inet parent 1:1 handle 10: netem delay {self.base_delay_ms}ms "
                        f"limit 100000", check=False)
            nsx(NS_INET, f"tc qdisc {verb} dev i-isp root netem delay {self.base_delay_ms}ms limit 100000",
                check=False)

    def set_capacity(self, down_mbps: float | None = None, up_mbps: float | None = None) -> None:
        self.apply_isp(down_mbps if down_mbps is not None else self.cur_down,
                       up_mbps if up_mbps is not None else self.cur_up)

    # ------------------------------------------------------------------ delay line
    def path_delay_ms(self, server: str) -> float:
        """One-way delay router<->server, identical to the simulator's base path."""
        return self.server_delay_ms(server) + self.access_delay_ms + self.lan_delay_ms

    def start_delayline(self) -> None:
        if self.delay_mode != "tun":
            return
        import multiprocessing as mp

        from . import delayline

        ctx = mp.get_context("spawn")
        self._dl_ready, self._dl_stop = ctx.Event(), ctx.Event()
        cfg = {"isp_ns": NS_ISP, "isp_dev": "x-inet", "inet_ns": NS_INET, "inet_dev": "i-isp",
               # epoll rounds timeouts up to the next millisecond: compensate ~1 ms per pass
               "delays_ms": {s.ip: max(0.0, self.path_delay_ms(s.name) - 1.0) for s in self.servers},
               "default_ms": 10.0}
        self._dl = ctx.Process(target=delayline.main, args=(cfg, self._dl_ready, self._dl_stop), daemon=True)
        self._dl.start()
        if not self._dl_ready.wait(10):
            raise RuntimeError("delay line failed to start")

    def stop_delayline(self) -> None:
        dl = getattr(self, "_dl", None)
        if dl is not None:
            self._dl_stop.set()
            dl.join(3)
            if dl.is_alive():
                dl.terminate()
            self._dl = None

    def extra_owd_ms(self, server: str) -> float:
        """Delay that must be added analytically (only when no delay line is running)."""
        if self.delay_mode == "tun":
            return 0.0
        return self.path_delay_ms(server) - self.base_delay_ms

    @staticmethod
    def teardown() -> None:
        for ns in list_ns():
            if ns.startswith(PREFIX):
                sh(f"ip netns del {ns}", check=False)

    @staticmethod
    def is_built() -> bool:
        return exists(NS_ROUTER) and exists(NS_INET)

    def device_ip(self, name: str) -> str:
        return next(d.ip for d in self.devices if d.name == name)

    def server_ip(self, name: str) -> str:
        return next(s.ip for s in self.servers if s.name == name)

    def server_delay_ms(self, name: str) -> float:
        return next(s.delay_ms for s in self.servers if s.name == name)
