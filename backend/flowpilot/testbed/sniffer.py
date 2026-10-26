"""Router-side flow monitor on real interfaces (AF_PACKET), run as its own process.

It listens to packets *arriving* at the home router - downlink packets on the WAN port
before the downlink shaper, uplink packets on the LAN port before the uplink shaper - so
it observes the offered load including packets the router later drops. Only the first
64 bytes of each packet are copied (IP + transport headers): FlowPilot never reads
payloads. Features are computed with the exact same `FlowMonitor` as the simulator and
sent to the controller every 500 ms.
"""
from __future__ import annotations

import ipaddress
import select
import socket
import struct
import time

from ..common.features import FlowMonitor, feature_vector
from .netns import enter_netns

ETH_P_IP = 0x0800
PACKET_OUTGOING = 4
SOL_PACKET = 263
PACKET_IGNORE_OUTGOING = 23


def _sock(dev: str) -> socket.socket:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_IP))
    s.bind((dev, 0))
    try:
        s.setsockopt(SOL_PACKET, PACKET_IGNORE_OUTGOING, 1)
    except OSError:
        pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
    s.setblocking(False)
    return s


def run(conn, ns: str, wan_dev: str, lan_dev: str, lan_net: str, ignore: tuple, interval: float = 0.5,
        feature_interval: float = 1.0, stop=None) -> None:
    enter_netns(ns)
    net = ipaddress.ip_network(lan_net)
    lan_prefix = int(net.network_address) & int(net.netmask)
    mask = int(net.netmask)
    socks = {_sock(wan_dev): False, _sock(lan_dev): True}  # value: packets on this port are uplink
    ep = select.epoll()
    for s in socks:
        ep.register(s.fileno(), select.EPOLLIN)
    by_fd = {s.fileno(): (s, up) for s, up in socks.items()}
    mon = FlowMonitor()
    buf = bytearray(96)
    view = memoryview(buf)
    unpack_ip = struct.Struct("!BBHHHBBH4s4s").unpack_from
    unpack_ports = struct.Struct("!HH").unpack_from
    ignore_set = {socket.inet_aton(ip) for ip in ignore}
    last_send = last_feat = time.monotonic()
    pkts = 0
    while stop is None or not stop.is_set():
        for fd, _ in ep.poll(0.05):
            s, up = by_fd[fd]
            while True:
                try:
                    n, addr = s.recvfrom_into(buf, 96)
                except BlockingIOError:
                    break
                if addr[2] == PACKET_OUTGOING or n < 20:
                    continue
                vihl, _tos, tot_len, _id, _frag, _ttl, proto, _ck, src, dst = unpack_ip(view)
                if src in ignore_set or dst in ignore_set:
                    continue
                ihl = (vihl & 0x0F) * 4
                if proto == 6:
                    p = "tcp"
                elif proto == 17:
                    p = "udp"
                else:
                    continue
                sport, dport = unpack_ports(view, ihl) if n >= ihl + 4 else (0, 0)
                if up:
                    lan_ip, wan_ip, lp, wp = src, dst, sport, dport
                else:
                    lan_ip, wan_ip, lp, wp = dst, src, dport, sport
                if int.from_bytes(lan_ip, "big") & mask != lan_prefix:
                    continue
                lip = socket.inet_ntoa(lan_ip)
                wip = socket.inet_ntoa(wan_ip)
                mon.observe((p, lip, lp, wip, wp), p, lip, wip, up, tot_len, time.monotonic())
                pkts += 1
        now = time.monotonic()
        if now - last_send >= interval:
            want_feat = now - last_feat >= feature_interval - 1e-6
            if want_feat:
                last_feat = now
                per_host, per_dst = mon.host_counts(now)
            flows = []
            for fs in mon.flows.values():
                if now - fs.last_ts > 2.0:
                    continue
                d = {"key": fs.key, "proto": fs.proto, "lan_ip": fs.lan_ip, "wan_ip": fs.wan_ip,
                     "first_ts": fs.first_ts, "last_ts": fs.last_ts, "bytes_up": fs.up.bytes,
                     "bytes_down": fs.down.bytes}
                if want_feat and fs.pkts >= 6:
                    d["vec"], d["info"] = feature_vector(fs, now, per_host.get(fs.lan_ip, 1),
                                                         per_dst.get((fs.lan_ip, fs.wan_ip), 1))
                flows.append(d)
            mon.expire(now)
            try:
                conn.send({"type": "flows", "t": now, "flows": flows, "pkts": pkts})
            except (BrokenPipeError, EOFError, OSError):
                return
            last_send = now


def main(conn, cfg: dict, stop=None) -> None:
    run(conn, cfg["ns"], cfg["wan_dev"], cfg["lan_dev"], cfg["lan_net"], tuple(cfg.get("ignore", ())),
        stop=stop)
