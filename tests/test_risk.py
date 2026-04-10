"""Tests for core/risk.py — exit rules (base + enhanced)."""

from datetime import date, timedelta

from core.risk import ExitContext, StopRules, evaluate_exit


TEN_DAYS = date.today() + timedelta(days=10)
TODAY = date.today()


# ─── Base rules ──────────────────────────────────────────────────────
def test_debit_target_hit():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=0.50, time_stop_dte=2, is_credit=False)
    # paid 1.00, mark 1.55 → +55%, hits target
    d = evaluate_exit(open_debit=1.00, current_mark=1.55,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules)
    assert d.should_exit
    assert d.reason == "target"


def test_debit_stop_hit():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=0.50, time_stop_dte=2, is_credit=False)
    d = evaluate_exit(open_debit=1.00, current_mark=0.40,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules)
    assert d.should_exit
    assert d.reason == "stop"


def test_credit_target_hit():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3, is_credit=True)
    d = evaluate_exit(open_debit=2.00, current_mark=0.95,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules)
    assert d.should_exit
    assert d.reason == "target"


def test_time_stop():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=0.50, time_stop_dte=2, is_credit=False)
    d = evaluate_exit(open_debit=1.00, current_mark=1.10,
                      expiry_date=date.today() + timedelta(days=2),
                      today=TODAY, rules=rules)
    assert d.should_exit
    assert d.reason == "time_stop"


def test_base_rules_work_with_no_context():
    """Backwards compat: evaluate_exit without context still works."""
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3, is_credit=True)
    d = evaluate_exit(open_debit=2.00, current_mark=2.00,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=None)
    assert not d.should_exit
    assert d.reason == "hold"


# ─── Vol-crush exit (credit only) ────────────────────────────────────
def _credit_rules_with_vol_crush(**over):
    kwargs = dict(
        profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
        is_credit=True, vol_crush_exit_pts=10,
    )
    kwargs.update(over)
    return StopRules(**kwargs)


def test_vol_crush_fires_on_large_iv_drop_when_profitable():
    rules = _credit_rules_with_vol_crush()
    # IV dropped from 90% to 78% = 12 pts drop, position is +$300
    ctx = ExitContext(
        entry_atm_iv=0.90, current_atm_iv=0.78,
        current_unrealized_pnl=300.0,
    )
    d = evaluate_exit(open_debit=2.00, current_mark=1.60,  # not at 50% target yet
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "vol_crush"
    assert "12" in (d.detail or "")  # mentions the drop magnitude


def test_vol_crush_does_not_fire_below_threshold():
    rules = _credit_rules_with_vol_crush(vol_crush_exit_pts=10)
    # IV dropped only 5 pts → below threshold
    ctx = ExitContext(
        entry_atm_iv=0.90, current_atm_iv=0.85,
        current_unrealized_pnl=300.0,
    )
    d = evaluate_exit(open_debit=2.00, current_mark=1.60,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit


def test_vol_crush_does_not_fire_when_losing():
    """Don't harvest a 'win' when we're actually red."""
    rules = _credit_rules_with_vol_crush()
    ctx = ExitContext(
        entry_atm_iv=0.90, current_atm_iv=0.70,      # 20pt drop — big
        current_unrealized_pnl=-200.0,                # but we're down $200
    )
    d = evaluate_exit(open_debit=2.00, current_mark=2.20,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit or d.reason != "vol_crush"


def test_vol_crush_disabled_when_rule_none():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, vol_crush_exit_pts=None)
    ctx = ExitContext(entry_atm_iv=0.90, current_atm_iv=0.60, current_unrealized_pnl=500.0)
    d = evaluate_exit(open_debit=2.00, current_mark=1.60,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit  # no enhanced rule, not at 50% target


def test_vol_crush_only_for_credit_trades():
    rules = StopRules(profit_target_pct=0.75, stop_loss_pct=0.50, time_stop_dte=2,
                      is_credit=False, vol_crush_exit_pts=10)
    ctx = ExitContext(entry_atm_iv=0.90, current_atm_iv=0.70, current_unrealized_pnl=500.0)
    d = evaluate_exit(open_debit=1.00, current_mark=1.20,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    # Debit trade — vol_crush rule should not fire
    assert not d.should_exit or d.reason != "vol_crush"


# ─── Short-strike tested (credit, defensive) ─────────────────────────
def test_short_strike_tested_defensive_close():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.50, time_stop_dte=3,
                      is_credit=True, short_strike_buffer_pct=0.015)
    # Short strike 90, underlying 90.5 (0.56% away, within 1.5% buffer)
    ctx = ExitContext(short_strike=90.0, current_underlying=90.5)
    d = evaluate_exit(open_debit=2.00, current_mark=2.00,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "strike_tested"
    assert d.urgency == "urgent"


def test_short_strike_not_tested_when_far():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.50, time_stop_dte=3,
                      is_credit=True, short_strike_buffer_pct=0.015)
    ctx = ExitContext(short_strike=90.0, current_underlying=95.0)  # 5.5% away
    d = evaluate_exit(open_debit=2.00, current_mark=2.00,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit


# ─── Trailing stop ───────────────────────────────────────────────────
def test_trailing_stop_activates_and_fires():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, trail_activate_pct=0.50, trail_giveback_pct=0.30)
    # open_debit=2.00, max_profit_proxy = 2000. activate at 50% = 1000.
    # peak = 1500 (activated). current = 1000. Gave back 500 = 33% of peak
    # → fires (≥ 30%)
    ctx = ExitContext(peak_unrealized_pnl=1500.0, current_unrealized_pnl=1000.0)
    d = evaluate_exit(open_debit=2.00, current_mark=1.50,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "trail"


def test_trailing_stop_inactive_below_activation():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, trail_activate_pct=0.50, trail_giveback_pct=0.30)
    # peak = 800 < activation threshold (1000)
    ctx = ExitContext(peak_unrealized_pnl=800.0, current_unrealized_pnl=500.0)
    d = evaluate_exit(open_debit=2.00, current_mark=1.70,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit


def test_trailing_stop_holds_when_still_near_peak():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, trail_activate_pct=0.50, trail_giveback_pct=0.30)
    # activated at 1500 peak, current 1400 → gave back 100/1500 = 6.7% (< 30%)
    ctx = ExitContext(peak_unrealized_pnl=1500.0, current_unrealized_pnl=1400.0)
    d = evaluate_exit(open_debit=2.00, current_mark=1.30,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit


# ─── Wide-spread defense ─────────────────────────────────────────────
def test_wide_spread_defers_target_exit():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, min_combo_spread_pct=0.25)
    # Target would hit (mark 0.95 = 52.5% of credit decayed), BUT combo is wide
    ctx = ExitContext(combo_bid=0.80, combo_ask=1.10)
    # mid = 0.95, spread_pct = 0.30 / 0.95 = 31% > 25% → defer
    d = evaluate_exit(open_debit=2.00, current_mark=0.95,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert not d.should_exit
    assert d.reason == "defer"


def test_wide_spread_does_not_defer_stop_loss():
    """Stops are urgent — wide spreads should NOT block them."""
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, min_combo_spread_pct=0.25)
    # Sold for 2.00, now marks 4.50 (stop loss at 2x credit = 4.00)
    ctx = ExitContext(combo_bid=4.00, combo_ask=5.00)  # very wide
    d = evaluate_exit(open_debit=2.00, current_mark=4.50,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "stop"


def test_narrow_spread_does_not_defer():
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.00, time_stop_dte=3,
                      is_credit=True, min_combo_spread_pct=0.25)
    # Tight spread — allow target
    ctx = ExitContext(combo_bid=0.93, combo_ask=0.97)
    d = evaluate_exit(open_debit=2.00, current_mark=0.95,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "target"


# ─── Rule priority ───────────────────────────────────────────────────
def test_time_stop_overrides_everything():
    """Time stop is unconditional — beats vol_crush, trail, strike_tested."""
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.50, time_stop_dte=3,
                      is_credit=True, vol_crush_exit_pts=10,
                      short_strike_buffer_pct=0.015,
                      trail_activate_pct=0.50, trail_giveback_pct=0.30)
    ctx = ExitContext(
        entry_atm_iv=0.90, current_atm_iv=0.60, current_unrealized_pnl=500.0,
        short_strike=90.0, current_underlying=90.5,
        peak_unrealized_pnl=1500.0,
    )
    d = evaluate_exit(open_debit=2.00, current_mark=1.60,
                      expiry_date=date.today() + timedelta(days=2),
                      today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "time_stop"


def test_strike_tested_beats_vol_crush():
    """Defensive close (urgent) fires before opportunistic take-profit."""
    rules = StopRules(profit_target_pct=0.50, stop_loss_pct=1.50, time_stop_dte=3,
                      is_credit=True, vol_crush_exit_pts=10,
                      short_strike_buffer_pct=0.015)
    ctx = ExitContext(
        entry_atm_iv=0.90, current_atm_iv=0.70, current_unrealized_pnl=200.0,
        short_strike=90.0, current_underlying=90.3,  # within buffer
    )
    d = evaluate_exit(open_debit=2.00, current_mark=1.80,
                      expiry_date=TEN_DAYS, today=TODAY, rules=rules, context=ctx)
    assert d.should_exit
    assert d.reason == "strike_tested"
