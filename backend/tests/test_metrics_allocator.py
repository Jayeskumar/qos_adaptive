import math

from flowpilot.common import metrics as M
from flowpilot.common.classes import TC
from flowpilot.controller.allocator import ClassDemand, allocate, weighted_waterfill


def test_emodel_reference_points():
    # no delay, no loss: near-toll quality (R ~ 93 -> MOS ~ 4.4)
    assert 4.3 < M.r_to_mos(M.emodel_r(0, 0)) <= 4.5
    # 150 ms mouth-to-ear is still good, 400 ms is poor (ITU-T G.114 planning limits)
    assert M.r_to_mos(M.emodel_r(150, 0)) > 4.0
    assert M.r_to_mos(M.emodel_r(400, 0)) < 3.6
    # loss hurts monotonically
    assert M.r_to_mos(M.emodel_r(50, 5)) < M.r_to_mos(M.emodel_r(50, 1)) < M.r_to_mos(M.emodel_r(50, 0))


def test_jain_and_qoe_fairness():
    assert M.jain([1, 1, 1]) == 1.0
    assert math.isclose(M.jain([1, 0, 0]), 1 / 3)
    assert M.qoe_fairness([4, 4, 4]) == 1.0
    assert M.qoe_fairness([1, 5]) == 0.0


def test_max_min_fair_shares():
    a = M.max_min_fair_shares({"a": 2, "b": 10, "c": math.inf}, 12)
    assert math.isclose(a["a"], 2) and math.isclose(a["b"], 5) and math.isclose(a["c"], 5)


def test_rfc3550_jitter_constant_delay_is_zero():
    j = M.Rfc3550Jitter()
    for i in range(100):
        j.update(i * 0.02, i * 0.02 + 0.03)
    assert j.j < 1e-9


def test_waterfill_respects_caps_floors_and_weights():
    x = weighted_waterfill(100, {1: (4, 0, 30), 2: (1, 5, math.inf), 3: (2, 5, 10)})
    assert math.isclose(sum(x.values()), 100)
    assert x[1] <= 30 + 1e-9 and x[3] <= 10 + 1e-9 and x[2] >= 5


def test_allocate_protects_inelastic_and_never_starves_bulk():
    R = 50e6
    d = {TC.REALTIME: ClassDemand(TC.REALTIME, nflows=1, arrival_bps=300e3, peak_bps=400e3),
         TC.CONFERENCING: ClassDemand(TC.CONFERENCING, nflows=1, arrival_bps=2.6e6, peak_bps=3e6),
         TC.STREAMING: ClassDemand(TC.STREAMING, nflows=1, arrival_bps=16e6, peak_bps=20e6, saturated=True, streams=1),
         TC.BULK: ClassDemand(TC.BULK, nflows=8, arrival_bps=30e6, peak_bps=30e6, saturated=True)}
    w = {TC.REALTIME: 8, TC.CONFERENCING: 8, TC.INTERACTIVE: 4, TC.STREAMING: 4, TC.BULK: 1}
    a = allocate(R, d, w)
    rates = {k: v.rate for k, v in a.classes.items()}
    assert rates[TC.REALTIME] >= 1.3 * 400e3  # peak + headroom
    assert rates[TC.CONFERENCING] >= 1.3 * 3e6
    assert rates[TC.BULK] >= 0.05 * R  # anti-starvation floor
    assert rates[TC.STREAMING] > rates[TC.BULK]  # weight 4 vs 1
    assert sum(rates.values()) <= R * 1.001
    # every elastic class can borrow the whole link (work conserving)
    assert a.classes[TC.BULK].ceil == R


def test_inelastic_cap_contains_misbehaving_class():
    R = 20e6
    d = {TC.REALTIME: ClassDemand(TC.REALTIME, nflows=1, arrival_bps=15e6, peak_bps=15e6)}
    a = allocate(R, d, {TC.REALTIME: 8})
    assert a.classes[TC.REALTIME].rate <= 0.20 * R + 1
