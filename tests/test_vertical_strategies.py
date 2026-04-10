"""Tests for vertical-spread strategies (bull/bear, debit/credit)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from strategies.bear_call_credit import BearCallCredit
from strategies.bear_put_debit import BearPutDebit
from strategies.bull_call_debit import BullCallDebit
from strategies.bull_put_credit import BullPutCredit


def _expiry_str(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y%m%d")


def make_chain(spot: float = 95.0, dte: int = 14, iv: float = 0.50) -> pd.DataFrame:
    """Build a synthetic LO chain centered on `spot`.

    Strikes from spot-15 to spot+15 in $1 increments. Mid prices and deltas
    are simple linear approximations sufficient for the strategy walker.
    """
    expiry = _expiry_str(dte)
    rows = []
    strikes = [round(spot - 15 + i, 1) for i in range(31)]
    for k in strikes:
        # Crude delta proxy: -0.5 ATM put → -0.05 deep OTM
        moneyness = (k - spot) / spot
        call_delta = max(0.02, min(0.98, 0.5 + moneyness * 4))
        put_delta = -max(0.02, min(0.98, 0.5 - moneyness * 4))

        # Crude price: intrinsic + uniform time value
        time_value = max(0.50, 2.0 - abs(moneyness) * 8)
        call_intrinsic = max(0.0, spot - k)
        put_intrinsic = max(0.0, k - spot)
        call_mid = call_intrinsic + time_value
        put_mid = put_intrinsic + time_value

        rows.append({
            "ts": datetime.now(),
            "sec_type": "FOP", "trading_class": "LO", "expiry": expiry,
            "strike": float(k), "right": "C",
            "local_symbol": "", "underlying_local_symbol": "CLM6",
            "bid": call_mid - 0.02, "ask": call_mid + 0.02, "mid": call_mid, "last": call_mid,
            "volume": 0, "open_interest": 100,
            "iv": iv, "delta": call_delta, "gamma": 0.01, "vega": 0.10, "theta": -0.02,
            "underlying_last": spot,
        })
        rows.append({
            "ts": datetime.now(),
            "sec_type": "FOP", "trading_class": "LO", "expiry": expiry,
            "strike": float(k), "right": "P",
            "local_symbol": "", "underlying_local_symbol": "CLM6",
            "bid": put_mid - 0.02, "ask": put_mid + 0.02, "mid": put_mid, "last": put_mid,
            "volume": 0, "open_interest": 100,
            "iv": iv, "delta": put_delta, "gamma": 0.01, "vega": 0.10, "theta": -0.02,
            "underlying_last": spot,
        })
    return pd.DataFrame(rows)


def common_credit_params() -> dict:
    return {
        "trading_class": "LO",
        "target_dte_min": 7,
        "target_dte_max": 21,
        "wing_offset": 5,
        "vol_filter_min_iv": 0.30,
        "min_credit_pct_of_width": 0.05,
        "default_qty": 1,
        "max_concurrent": 1,
    }


def common_debit_params() -> dict:
    return {
        "trading_class": "LO",
        "target_dte_min": 7,
        "target_dte_max": 21,
        "width": 5,
        "max_iv": 0.95,
        "max_debit_pct_of_width": 0.95,
        "default_qty": 1,
        "max_concurrent": 1,
    }


# ─── BullPutCredit ─────────────────────────────────────────────────────
def test_bull_put_credit_emits_signal():
    chain = make_chain(spot=95, dte=14, iv=0.55)
    strat = BullPutCredit(
        id="t", name="t", tier="paper", enabled=True,
        params={**common_credit_params(), "short_put_delta": -0.25},
    )
    signals = strat.evaluate({"snapshot": chain}, [])
    assert len(signals) == 1
    sig = signals[0]
    assert sig.structure == "bull_put_credit_spread"
    assert len(sig.legs) == 2
    # Short put at higher K, long put at lower K
    sell_leg = next(l for l in sig.legs if l.action == "SELL")
    buy_leg = next(l for l in sig.legs if l.action == "BUY")
    assert sell_leg.instrument.strike > buy_leg.instrument.strike
    assert sig.target_debit < 0  # credit
    assert sig.max_loss > 0


def test_bull_put_credit_skips_low_iv():
    chain = make_chain(spot=95, dte=14, iv=0.20)
    strat = BullPutCredit(
        id="t", name="t", tier="paper", enabled=True,
        params={**common_credit_params(), "vol_filter_min_iv": 0.40},
    )
    assert strat.evaluate({"snapshot": chain}, []) == []


# ─── BearCallCredit ────────────────────────────────────────────────────
def test_bear_call_credit_emits_signal():
    chain = make_chain(spot=95, dte=14, iv=0.55)
    strat = BearCallCredit(
        id="t", name="t", tier="paper", enabled=True,
        params={**common_credit_params(), "short_call_delta": 0.25},
    )
    signals = strat.evaluate({"snapshot": chain}, [])
    assert len(signals) == 1
    sig = signals[0]
    assert sig.structure == "bear_call_credit_spread"
    sell_leg = next(l for l in sig.legs if l.action == "SELL")
    buy_leg = next(l for l in sig.legs if l.action == "BUY")
    assert sell_leg.instrument.strike < buy_leg.instrument.strike  # short < long for calls
    assert sig.target_debit < 0


# ─── BullCallDebit ─────────────────────────────────────────────────────
def test_bull_call_debit_emits_signal():
    chain = make_chain(spot=95, dte=14, iv=0.45)
    strat = BullCallDebit(
        id="t", name="t", tier="experimental", enabled=True,
        params={**common_debit_params(), "long_call_delta": 0.50},
    )
    signals = strat.evaluate({"snapshot": chain}, [])
    assert len(signals) == 1
    sig = signals[0]
    assert sig.structure == "bull_call_debit_spread"
    buy_leg = next(l for l in sig.legs if l.action == "BUY")
    sell_leg = next(l for l in sig.legs if l.action == "SELL")
    assert buy_leg.instrument.strike < sell_leg.instrument.strike
    assert sig.target_debit > 0  # debit


def test_bull_call_debit_skips_high_iv():
    chain = make_chain(spot=95, dte=14, iv=0.95)
    strat = BullCallDebit(
        id="t", name="t", tier="experimental", enabled=True,
        params={**common_debit_params(), "max_iv": 0.80},
    )
    assert strat.evaluate({"snapshot": chain}, []) == []


# ─── BearPutDebit ──────────────────────────────────────────────────────
def test_bear_put_debit_emits_signal():
    chain = make_chain(spot=95, dte=14, iv=0.45)
    strat = BearPutDebit(
        id="t", name="t", tier="experimental", enabled=True,
        params={**common_debit_params(), "long_put_delta": -0.50},
    )
    signals = strat.evaluate({"snapshot": chain}, [])
    assert len(signals) == 1
    sig = signals[0]
    assert sig.structure == "bear_put_debit_spread"
    buy_leg = next(l for l in sig.legs if l.action == "BUY")
    sell_leg = next(l for l in sig.legs if l.action == "SELL")
    assert buy_leg.instrument.strike > sell_leg.instrument.strike
    assert sig.target_debit > 0


# ─── max_concurrent ────────────────────────────────────────────────────
def test_max_concurrent_caps_signals():
    chain = make_chain(spot=95, dte=14, iv=0.55)
    strat = BullPutCredit(
        id="t", name="t", tier="paper", enabled=True,
        params={**common_credit_params(), "max_concurrent": 1},
    )
    fake_existing = [{"id": 1, "structure": "bull_put_credit_spread"}]
    assert strat.evaluate({"snapshot": chain}, fake_existing) == []


def test_disabled_strategy_emits_nothing():
    chain = make_chain(spot=95, dte=14, iv=0.55)
    strat = BullPutCredit(
        id="t", name="t", tier="paper", enabled=False,
        params=common_credit_params(),
    )
    assert strat.evaluate({"snapshot": chain}, []) == []
