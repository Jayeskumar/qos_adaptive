"""Linux network-namespace helpers.

The testbed is a set of named network namespaces wired with veth pairs. A single Python
process can own sockets in many namespaces: `setns()` switches only the calling thread,
and a socket stays bound to the namespace it was created in, so we switch, create the
socket, and switch back. All traffic generators therefore run in one asyncio loop.
"""
from __future__ import annotations

import contextlib
import functools
import itertools
import os
import shlex
import socket
import subprocess
import threading
from typing import Iterator, Optional

NETNS_DIR = "/var/run/netns"
_lock = threading.Lock()


class CmdError(RuntimeError):
    pass


def sh(cmd: str, check: bool = True, input: Optional[str] = None, timeout: float = 20.0) -> str:
    p = subprocess.run(shlex.split(cmd), input=input, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise CmdError(f"{cmd!r} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
    return p.stdout


def nsx(ns: str, cmd: str, check: bool = True, input: Optional[str] = None) -> str:
    """Run a command inside namespace `ns`."""
    return sh(f"ip netns exec {ns} {cmd}", check=check, input=input)


def exists(ns: str) -> bool:
    return os.path.exists(os.path.join(NETNS_DIR, ns))


def list_ns() -> list[str]:
    try:
        return sorted(os.listdir(NETNS_DIR))
    except FileNotFoundError:
        return []


@contextlib.contextmanager
def in_netns(ns: str) -> Iterator[None]:
    """Temporarily move the *current thread* into namespace `ns`."""
    with _lock:
        target = os.open(os.path.join(NETNS_DIR, ns), os.O_RDONLY)
        orig = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
        try:
            os.setns(target, os.CLONE_NEWNET)
            yield
        finally:
            os.setns(orig, os.CLONE_NEWNET)
            os.close(target)
            os.close(orig)


def enter_netns(ns: str) -> None:
    """Permanently move the current (single-threaded) process into `ns`."""
    fd = os.open(os.path.join(NETNS_DIR, ns), os.O_RDONLY)
    try:
        os.setns(fd, os.CLONE_NEWNET)
    finally:
        os.close(fd)


def ns_socket(ns: str, family: int = socket.AF_INET, type_: int = socket.SOCK_DGRAM,
              bind: Optional[tuple] = None, reuse: bool = True) -> socket.socket:
    with in_netns(ns):
        s = socket.socket(family, type_)
        if reuse:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if bind is not None:
            s.bind(bind)
    s.setblocking(False)
    return s


_probe_ids = itertools.count()


@functools.lru_cache(maxsize=None)
def qdisc_supported(kind: str) -> bool:
    """Probe whether the running kernel offers a queueing discipline (cached per process)."""
    tmp = f"fpprobe{os.getpid()}-{next(_probe_ids)}"  # unique: the API and a benchmark may probe at once
    try:
        sh(f"ip netns add {tmp}")
        nsx(tmp, "ip link add fpd0 type veth peer name fpd1")
        args = {"tbf": "rate 1mbit burst 10k latency 10ms", "netem": "delay 1ms"}.get(kind, "")
        r = subprocess.run(shlex.split(f"ip netns exec {tmp} tc qdisc add dev fpd0 root handle 1: {kind} {args}"),
                           capture_output=True, text=True)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        sh(f"ip netns del {tmp}", check=False)


def capabilities() -> dict:
    caps = {"root": os.geteuid() == 0, "netns": False, "qdiscs": {}}
    if not caps["root"]:
        return caps
    try:
        sh("ip -V")
        caps["netns"] = True
    except Exception:
        return caps
    for q in ("htb", "tbf", "bfifo", "fq_codel", "cake", "netem"):
        caps["qdiscs"][q] = qdisc_supported(q)
    return caps
