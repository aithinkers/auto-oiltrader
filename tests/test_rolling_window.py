"""Tests for core/rolling_window.py — contract roll lifecycle."""

from datetime import date, datetime, timedelta

from core.rolling_window import (
    ActiveWindow,
    ContractInfo,
    compute_active_window,
    diff_windows,
    needs_rebuild,
)


def make_contract(local_symbol: str, expiry_str: str, con_id: int = 0) -> ContractInfo:
    return ContractInfo(
        symbol="CL",
        local_symbol=local_symbol,
        expiry=expiry_str,
        con_id=con_id,
    )


def test_basic_three_month_window():
    today = date(2026, 4, 9)
    contracts = [
        make_contract("CLK6", "20260421"),  # +12d  -> tradeable
        make_contract("CLM6", "20260519"),  # +40d  -> tradeable
        make_contract("CLN6", "20260622"),  # +74d  -> tradeable
        make_contract("CLO6", "20260721"),  # +103d -> NOT in window (n=3)
    ]
    w = compute_active_window(contracts, set(), n_months_ahead=3, drop_when_dte_le=5, today=today)
    assert [c.local_symbol for c in w.tradeable] == ["CLK6", "CLM6", "CLN6"]
    assert w.markable == []


def test_drop_when_close_to_expiry_no_position():
    today = date(2026, 4, 17)  # CLK6 expires Apr 21 → DTE=4, ≤ drop=5
    contracts = [
        make_contract("CLK6", "20260421"),
        make_contract("CLM6", "20260519"),
        make_contract("CLN6", "20260622"),
        make_contract("CLO6", "20260721"),
    ]
    w = compute_active_window(contracts, set(), n_months_ahead=3, drop_when_dte_le=5, today=today)
    # CLK6 dropped, CLO6 promoted into the window
    assert [c.local_symbol for c in w.tradeable] == ["CLM6", "CLN6", "CLO6"]
    assert w.markable == []


def test_drop_with_open_position_keeps_markable():
    today = date(2026, 4, 17)
    contracts = [
        make_contract("CLK6", "20260421"),
        make_contract("CLM6", "20260519"),
        make_contract("CLN6", "20260622"),
        make_contract("CLO6", "20260721"),
    ]
    w = compute_active_window(
        contracts,
        open_position_locals={"CLK6"},
        n_months_ahead=3,
        drop_when_dte_le=5,
        today=today,
    )
    assert [c.local_symbol for c in w.tradeable] == ["CLM6", "CLN6", "CLO6"]
    assert [c.local_symbol for c in w.markable] == ["CLK6"]


def test_expired_contracts_removed():
    today = date(2026, 4, 22)  # CLK6 expired yesterday
    contracts = [
        make_contract("CLK6", "20260421"),
        make_contract("CLM6", "20260519"),
        make_contract("CLN6", "20260622"),
        make_contract("CLO6", "20260721"),
    ]
    w = compute_active_window(contracts, set(), n_months_ahead=3, drop_when_dte_le=5, today=today)
    assert [c.local_symbol for c in w.tradeable] == ["CLM6", "CLN6", "CLO6"]
    assert w.markable == []


def test_expired_contract_with_position_still_dropped():
    """Expired contracts always drop regardless of positions — can't trade them anyway."""
    today = date(2026, 4, 22)
    contracts = [
        make_contract("CLK6", "20260421"),
        make_contract("CLM6", "20260519"),
    ]
    w = compute_active_window(
        contracts, open_position_locals={"CLK6"},
        n_months_ahead=3, drop_when_dte_le=5, today=today,
    )
    # CLK6 is gone entirely — past expiry, no longer markable
    assert all(c.local_symbol != "CLK6" for c in w.tradeable)
    assert all(c.local_symbol != "CLK6" for c in w.markable)


def test_diff_windows_detects_roll():
    old = ActiveWindow(
        tradeable=[make_contract("CLK6", "20260421"), make_contract("CLM6", "20260519")],
        markable=[],
        computed_at=datetime.now(),
        n_months_ahead=2,
        drop_when_dte_le=5,
    )
    new = ActiveWindow(
        tradeable=[make_contract("CLM6", "20260519"), make_contract("CLN6", "20260622")],
        markable=[],
        computed_at=datetime.now(),
        n_months_ahead=2,
        drop_when_dte_le=5,
    )
    d = diff_windows(old, new)
    assert d["added"] == ["CLN6"]
    assert d["removed"] == ["CLK6"]
    assert d["unchanged"] == ["CLM6"]


def test_needs_rebuild_no_window():
    assert needs_rebuild(None, datetime(2026, 4, 9, 6, 0), rebuild_hour_et=6)


def test_needs_rebuild_front_month_aged_out():
    today = date(2026, 4, 17)
    w = ActiveWindow(
        tradeable=[make_contract("CLK6", "20260421")],  # DTE=4
        markable=[],
        computed_at=datetime(2026, 4, 16, 6, 0),
        n_months_ahead=3,
        drop_when_dte_le=5,
    )
    assert needs_rebuild(w, datetime(2026, 4, 17, 7, 0), rebuild_hour_et=6)


def test_no_rebuild_within_same_day():
    w = ActiveWindow(
        tradeable=[make_contract("CLK6", "20260421")],  # DTE=12
        markable=[],
        computed_at=datetime(2026, 4, 9, 6, 0),
        n_months_ahead=3,
        drop_when_dte_le=5,
    )
    # Same day, after rebuild hour, window is still healthy
    assert not needs_rebuild(w, datetime(2026, 4, 9, 14, 0), rebuild_hour_et=6)
