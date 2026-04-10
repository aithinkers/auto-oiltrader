"""Tests for strategies/base.py and strategies/iron_condor_range.py."""

from strategies.iron_condor_range import IronCondorRange


def test_strategy_can_open_more():
    s = IronCondorRange(
        id="test",
        name="Test",
        tier="experimental",
        enabled=True,
        params={"max_concurrent": 3},
    )
    assert s.can_open_more([])
    assert s.can_open_more([{}, {}])
    assert not s.can_open_more([{}, {}, {}])


def test_strategy_returns_empty_when_disabled():
    s = IronCondorRange(
        id="test",
        name="Test",
        tier="experimental",
        enabled=False,
        params={},
    )
    import pandas as pd
    state = {"snapshot": pd.DataFrame()}
    assert s.evaluate(state, []) == []


def test_strategy_returns_empty_when_no_data():
    s = IronCondorRange(
        id="test",
        name="Test",
        tier="experimental",
        enabled=True,
        params={},
    )
    import pandas as pd
    state = {"snapshot": pd.DataFrame()}
    assert s.evaluate(state, []) == []
