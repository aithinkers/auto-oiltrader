"""Tests for core/combos.py and core/contracts.py."""

from core.combos import iron_condor_math, call_butterfly_math, put_debit_spread_math
from core.contracts import iron_condor_legs, call_butterfly_legs, put_debit_spread_legs


def test_iron_condor_math():
    m = iron_condor_math(short_put_k=92, long_put_k=88, short_call_k=100, long_call_k=104,
                         net_credit=1.40, qty=1)
    assert m.is_debit is False
    assert m.max_profit == 1400
    assert m.max_loss == (4 - 1.40) * 1000
    assert m.upper_be == 101.40
    assert m.lower_be == 90.60


def test_call_butterfly_math():
    m = call_butterfly_math(lower_k=95, body_k=100, upper_k=105, net_debit=0.60, qty=1)
    assert m.is_debit
    assert m.max_profit == (5 - 0.60) * 1000
    assert m.max_loss == 600


def test_put_debit_spread_math():
    m = put_debit_spread_math(long_k=95, short_k=90, net_debit=1.60, qty=1)
    assert m.max_profit == (5 - 1.60) * 1000
    assert m.max_loss == 1600
    assert abs(m.upper_be - 93.40) < 0.001


def test_iron_condor_legs_count():
    legs = iron_condor_legs("LO", "20260416", short_put_k=92, long_put_k=88,
                            short_call_k=100, long_call_k=104, qty=1)
    assert len(legs) == 4


def test_call_butterfly_legs_ratio():
    legs = call_butterfly_legs("LO", "20260416", lower_k=95, body_k=100, upper_k=105, qty=1)
    assert len(legs) == 3
    assert legs[1].ratio == 2  # body
