"""Userspace WAN delay line: real per-destination path delay without `netem`.

The Internet namespace and the ISP namespace are joined by two TUN devices instead of a
veth pair. This relay process reads IP packets from one TUN and writes them to the other
after the one-way path delay of the server involved (looked up by IP address), so every
TCP connection in the testbed experiences a realistic RTT (e.g. 2 x 14.5 ms to the game
server) and congestion control behaves as on the real Internet.

Packets with equal delay keep FIFO order, so there is no reordering; precision is ~1 ms
(epoll granularity), far below the delays being emulated.
"""
from __future__ import annotations

import errno
import fcntl
import os
import select
import struct
import time
from collections import deque

from .netns import in_netns

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000


def open_tun(ns: str, name: str) -> int:
    with in_netns(ns):
        fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
        ifr = struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI)
        fcntl.ioctl(fd, TUNSETIFF, ifr)
    return fd


def _ip(b: bytes) -> str:
    return ".".join(str(x) for x in b)


def run(isp_ns: str, isp_dev: str, inet_ns: str, inet_dev: str, delays_ms: dict, default_ms: float,
        ready=None, stop=None) -> None:
    fd_isp = open_tun(isp_ns, isp_dev)  # packets *from* the ISP side = uplink, towards servers
    fd_inet = open_tun(inet_ns, inet_dev)  # packets *from* servers = downlink, towards the home
    if ready is not None:
        ready.set()
    d_s = {ip: ms / 1000.0 for ip, ms in delays_ms.items()}
    default = default_ms / 1000.0
    lines: dict[tuple[int, float], deque] = {}
    ep = select.epoll()
    ep.register(fd_isp, select.EPOLLIN)
    ep.register(fd_inet, select.EPOLLIN)
    stats = {"up": 0, "down": 0, "drops": 0}
    while stop is None or not stop.is_set():
        now = time.monotonic()
        nxt = None
        for (out_fd, _), q in lines.items():
            while q and q[0][0] <= now:
                try:
                    os.write(out_fd, q[0][1])
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EIO, errno.ENOBUFS):
                        raise
                    stats["drops"] += 1
                q.popleft()
            if q and (nxt is None or q[0][0] < nxt):
                nxt = q[0][0]
        timeout = 0.05 if nxt is None else max(0.0, nxt - time.monotonic())
        for fd, _ev in ep.poll(timeout):
            t = time.monotonic()
            while True:
                try:
                    pkt = os.read(fd, 65535)
                except BlockingIOError:
                    break
                if len(pkt) < 20 or pkt[0] >> 4 != 4:
                    continue
                if fd == fd_isp:  # uplink: delay by destination server
                    d = d_s.get(_ip(pkt[16:20]), default)
                    out = fd_inet
                    stats["up"] += 1
                else:  # downlink: delay by source server
                    d = d_s.get(_ip(pkt[12:16]), default)
                    out = fd_isp
                    stats["down"] += 1
                key = (out, d)
                q = lines.get(key)
                if q is None:
                    q = lines[key] = deque()
                q.append((t + d, pkt))


def main(cfg: dict, ready=None, stop=None) -> None:  # multiprocessing entry point
    try:
        os.nice(-5)
    except OSError:
        pass
    run(cfg["isp_ns"], cfg["isp_dev"], cfg["inet_ns"], cfg["inet_dev"], cfg["delays_ms"], cfg["default_ms"],
        ready, stop)
