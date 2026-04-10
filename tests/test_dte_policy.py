"""Tests for core/dte_policy.py — scenario-based DTE floor."""

import pytest

from core.dte_policy import (
    BLOCK_ALL,
    DTEPolicyConfig,
    MarketState,
    Scenario,
    TradeContext,
    min_dte_for_new_position,
)


@pytest.fixture
def cfg():
    return DTEPolicyConfig()


# ----- HARD-NO CONDITIONS -----

def test_hard_no_dte_zero(cfg):
    ctx = TradeContext(structure="put_spread", is_credit=False)
    state = MarketState(proposed_dte=0, atm_iv=0.40, realized_vol_10d=0.30)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
    assert "DTE = 0" in d.reason


def test_hard_no_extreme_realized_vol(cfg):
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(proposed_dte=10, atm_iv=0.95, realized_vol_10d=1.20)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
    assert "Realized vol" in d.reason


def test_hard_no_wide_combo_spread(cfg):
    ctx = TradeContext(structure="butterfly", is_credit=False)
    state = MarketState(proposed_dte=7, atm_iv=0.60, combo_bid_ask_pct=0.40)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
    assert "bid/ask" in d.reason


def test_hard_no_fast_move(cfg):
    ctx = TradeContext(structure="put_spread", is_credit=False)
    state = MarketState(proposed_dte=10, atm_iv=0.60, iv_change_60min_pts=4.5)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
    assert "vol pts in last 60 min" in d.reason


def test_hard_no_critical_news(cfg):
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(proposed_dte=14, atm_iv=0.60, has_critical_news_within_hours=0)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
    assert "Critical news" in d.reason


# ----- HEDGE SCENARIO -----

def test_hedge_lifts_floor(cfg):
    ctx = TradeContext(structure="naked_call", is_credit=False, is_hedge=True,
                       proposed_size_pct_of_book=0.20)
    state = MarketState(proposed_dte=3, atm_iv=0.95, realized_vol_10d=0.60)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert not d.blocking
    assert d.scenario == Scenario.HEDGE
    assert d.min_dte == 1


def test_hedge_too_large_blocks(cfg):
    ctx = TradeContext(structure="naked_call", is_credit=False, is_hedge=True,
                       proposed_size_pct_of_book=0.75)
    state = MarketState(proposed_dte=3, atm_iv=0.60, realized_vol_10d=0.40)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
    assert "Hedge size" in d.reason


def test_hedge_still_blocks_on_hard_no(cfg):
    """Hard-NO conditions override even hedges."""
    ctx = TradeContext(structure="naked_call", is_credit=False, is_hedge=True,
                       proposed_size_pct_of_book=0.20)
    state = MarketState(proposed_dte=3, atm_iv=0.95, realized_vol_10d=1.50)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking  # extreme RV blocks even hedges


# ----- PIN TRADE SCENARIO -----

def test_pin_trade_normal(cfg):
    ctx = TradeContext(structure="butterfly", is_credit=False, is_pin_trade=True)
    state = MarketState(proposed_dte=5, atm_iv=0.60)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PIN_TRADE
    assert d.min_dte == 5


def test_pin_trade_strong_magnet_lower_floor(cfg):
    ctx = TradeContext(structure="butterfly", is_credit=False, is_pin_trade=True,
                       has_strong_oi_magnet=True)
    state = MarketState(proposed_dte=4, atm_iv=0.60)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PIN_TRADE_STRONG_MAGNET
    assert d.min_dte == 3


# ----- PREMIUM-SELL SCENARIOS -----

def test_premium_sell_high_vol_regime(cfg):
    """IV>80% AND RV>50% → high-vol floor (14 DTE)."""
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(proposed_dte=10, atm_iv=0.95, realized_vol_10d=0.60)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PREMIUM_SELL_HIGH_VOL
    assert d.min_dte == 14


def test_premium_sell_calm_sweet_spot(cfg):
    """IV>50% AND RV<35% → sweet spot (5 DTE)."""
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(proposed_dte=7, atm_iv=0.60, realized_vol_10d=0.30)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PREMIUM_SELL_CALM
    assert d.min_dte == 5


def test_premium_sell_low_iv(cfg):
    """IV<30% → standard floor."""
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(proposed_dte=10, atm_iv=0.25, realized_vol_10d=0.20)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PREMIUM_SELL_NORMAL
    assert d.min_dte == 10


def test_premium_sell_with_recent_critical_news(cfg):
    """Critical news in last 24h extends the floor for credit trades."""
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(proposed_dte=14, atm_iv=0.60, realized_vol_10d=0.30,
                        has_critical_news_within_hours=20)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert not d.blocking  # not within 1h block window
    assert d.scenario == Scenario.PREMIUM_SELL_HIGH_VOL
    assert d.min_dte > cfg.premium_sell_min_dte_high_vol  # extra cushion


# ----- LONG DEBIT SCENARIOS -----

def test_long_debit_normal(cfg):
    ctx = TradeContext(structure="put_spread", is_credit=False)
    state = MarketState(proposed_dte=7, atm_iv=0.40)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.LONG_DEBIT_NORMAL
    assert d.min_dte == 5


def test_long_debit_high_iv(cfg):
    """IV > 80% pushes long debit floor up."""
    ctx = TradeContext(structure="call_spread", is_credit=False)
    state = MarketState(proposed_dte=7, atm_iv=0.95)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.LONG_DEBIT_HIGH_IV
    assert d.min_dte == 7


def test_long_debit_wide_spread(cfg):
    """Wide combo spread (>10%) pushes floor up."""
    ctx = TradeContext(structure="put_spread", is_credit=False)
    state = MarketState(proposed_dte=7, atm_iv=0.40, combo_bid_ask_pct=0.15)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.LONG_DEBIT_WIDE_SPREAD
    assert d.min_dte == 10


# ----- SCHEDULED EVENT SCENARIO -----

def test_scheduled_event_play(cfg):
    ctx = TradeContext(structure="strangle", is_credit=False, is_scheduled_event_play=True)
    state = MarketState(proposed_dte=2, atm_iv=0.50)
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.SCHEDULED_EVENT
    assert d.min_dte == 1


# ----- INTEGRATION-STYLE: real-world examples from the conversation -----

def test_current_state_iron_condor_apr16(cfg):
    """Apr 9 evening, CL=$97, IV~95%, recent crash → refuse short DTE credit."""
    ctx = TradeContext(structure="iron_condor", is_credit=True)
    state = MarketState(
        proposed_dte=7,           # LO Apr16
        atm_iv=0.95,
        realized_vol_10d=0.83,
        iv_change_60min_pts=1.5,  # not fast-move
    )
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PREMIUM_SELL_HIGH_VOL
    assert d.min_dte == 14
    # 7 DTE < 14 floor → trade would be rejected
    assert state.proposed_dte < d.min_dte


def test_current_state_butterfly_apr16(cfg):
    """Same conditions, but it's a pin-trade butterfly → allowed."""
    ctx = TradeContext(structure="butterfly", is_credit=False, is_pin_trade=True)
    state = MarketState(
        proposed_dte=7,
        atm_iv=0.95,
        realized_vol_10d=0.83,
    )
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.scenario == Scenario.PIN_TRADE
    assert d.min_dte == 5
    assert state.proposed_dte >= d.min_dte  # 7 >= 5 → allowed


def test_current_state_may14_credit_spread(cfg):
    """May 14 expiry (~36 DTE) — well above any short DTE floor."""
    ctx = TradeContext(structure="call_spread", is_credit=True)
    state = MarketState(
        proposed_dte=36,
        atm_iv=0.92,
        realized_vol_10d=0.70,
    )
    d = min_dte_for_new_position(ctx, state, cfg)
    assert state.proposed_dte >= d.min_dte
    assert not d.blocking


def test_current_state_naked_short_futures_blocked(cfg):
    """Adding a new naked short while RV > 100% → blocked."""
    # Encoded as a credit trade with extreme RV — same hard-no path
    ctx = TradeContext(structure="naked_short", is_credit=True)
    state = MarketState(
        proposed_dte=12,
        atm_iv=1.10,
        realized_vol_10d=1.05,
    )
    d = min_dte_for_new_position(ctx, state, cfg)
    assert d.blocking
