"""Data-plane models behave like their Linux counterparts, and the simulator reproduces
the phenomena the project is about (bufferbloat, and FlowPilot removing it)."""
from flowpilot.bench.runner import run_sim
from flowpilot.sim.engine import Sim
from flowpilot.sim.net import K_UDP, Flow, Link, Packet
from flowpilot.sim.qdisc import FIFO, HTB, FQCoDel


def _flow(i, ip="192.168.1.10"):
    return Flow(i, "udp", ip, 1000 + i, "203.0.113.1", 9, "dev", "x", "x", 0.0)


def _saturate(sim, link, flows, size=1500, rate_pps=20000, until=2.0):
    def gen(f):
        link.arrive(Packet(f, size, K_UDP, False, sim.now))
        if sim.now < until:
            sim.after(1.0 / rate_pps, gen, f)

    for f in flows:
        sim.after(0.0, gen, f)


def test_fifo_tail_drop():
    q = FIFO(limit_pkts=2)
    f = _flow(1)
    assert q.enqueue(Packet(f, 100, K_UDP, False, 0), 0)
    assert q.enqueue(Packet(f, 100, K_UDP, False, 0), 0)
    assert not q.enqueue(Packet(f, 100, K_UDP, False, 0), 0)
    assert q.drops == 1


def test_htb_rates_ceil_and_priority_borrowing():
    sim = Sim()
    got = {}
    h = HTB(10e6)
    h.add_class(10, 1, 1e6, 10e6, 0)  # high prio: should win the 4 Mb/s of spare capacity
    h.add_class(20, 1, 4e6, 10e6, 1)
    h.add_class(30, 1, 1e6, 3e6, 2)  # capped by its ceil
    flows = {10: _flow(10), 20: _flow(20), 30: _flow(30)}
    h.classify = lambda p: p.flow.fid
    link = Link(sim, "l", 1e9, 0.0, h, next_hop=lambda p: got.__setitem__(p.flow.fid, got.get(p.flow.fid, 0) + p.size))
    _saturate(sim, link, flows.values())
    sim.run_until(2.0)
    tot = sum(got.values()) * 8 / 2.0
    assert 9.0e6 < tot < 10.6e6  # root shapes the aggregate
    r = {k: v * 8 / 2.0 for k, v in got.items()}
    assert r[30] < 3.3e6  # ceil respected
    assert r[10] > 4.5e6  # the prio-0 class borrows the spare bandwidth first
    assert 3.6e6 < r[20] < 4.6e6  # its guarantee, no more (lower borrowing priority)
    assert r[30] > 0.9e6  # guarantees respected even for the lowest class


def test_fq_codel_isolates_sparse_flow():
    sim = Sim()
    delays = []
    q = FQCoDel()

    def deliver(p):
        if p.flow.fid == 2:
            delays.append(sim.now - p.ts)

    link = Link(sim, "l", 10e6, 0.0, q, next_hop=deliver)
    _saturate(sim, link, [_flow(1)], rate_pps=2000, until=3.0)  # 24 Mb/s into 10 Mb/s

    def sparse():
        link.arrive(Packet(_flow(2, "192.168.1.11"), 200, K_UDP, False, sim.now))
        if sim.now < 3.0:
            sim.after(0.02, sparse)

    sim.after(0.5, sparse)
    sim.run_until(3.2)
    assert delays and max(delays[10:]) < 0.005  # a sparse flow never waits behind the bulk flow


def test_bufferbloat_reproduced_and_removed():
    fifo = run_sim("evening_rush", "fifo", duration=45, keep_series=False)["summary"]
    fp = run_sim("evening_rush", "flowpilot", duration=45, keep_series=False)["summary"]
    assert fifo["game_rtt_p50_ms"] > 120  # standard router: bufferbloat once the download starts
    assert fp["game_rtt_p99_ms"] < 70  # FlowPilot: near the ~29 ms base RTT
    assert fp["call_mos"] > 4.0 > fifo["call_mos"]
