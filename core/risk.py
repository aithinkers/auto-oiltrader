"""Stop-loss / profit-target rules and exit evaluation.

The exit evaluator is pure, deterministic, and runs every position_manager tick.
It supports two families of rules:

  1. Base rules (always active):
       profit_target_pct, stop_loss_pct, time_stop_dte
     These are the original simple rules — exit at X% gain or X% loss or
     when DTE crosses a floor.

  2. Enhanced rules (optional; None = disabled):
       vol_crush_exit_pts       — credit trades, exit early if IV drops
       trail_activate_pct       — activate a trailing stop after N% gain
       trail_giveback_pct       — trailing stop retrace threshold
       short_strike_buffer_pct  — credit trades, defensive close on strike test
       min_combo_spread_pct     — defer target exits when liquidity is bad

Enhanced rules need an `ExitContext` populated by the position_manager before
the call. If context is None, only base rules apply (backwards-compatible).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str
    urgency: str = "normal"      # normal | urgent | critical
    detail: str | None = None    # optional human-readable explanation


@dataclass
class StopRules:
    """Per-strategy exit configuration. Loaded from strategies.yaml params."""
    profit_target_pct: float
    stop_loss_pct: float
    time_stop_dte: int
    is_credit: bool = False

    # ── Enhanced rules — all optional ──────────────────────────────────
    # Credit-trade vol-crush early profit-take. If ATM IV on this position's
    # (class, expiry) has dropped by MORE than this many vol points since
    # entry AND the position is currently profitable, take profit regardless
    # of the % target.
    vol_crush_exit_pts: Optional[float] = None

    # Trailing stop: once unrealized_pnl has reached at least
    # trail_activate_pct × (open_debit × 1000) — an approximate max-profit
    # proxy — start tracking peak_unrealized_pnl. Exit when current pnl
    # retraces by trail_giveback_pct of the peak.
    trail_activate_pct: Optional[float] = None
    trail_giveback_pct: Optional[float] = None

    # Defensive close for credit trades: if the underlying moves within
    # this fraction of the short strike, close early before the strike
    # gets tested and gamma explodes.
    short_strike_buffer_pct: Optional[float] = None

    # Wide-spread defense: if the combo bid/ask is wider than this fraction
    # of its mid, DEFER target exits (but not stops). Prevents bad fills.
    min_combo_spread_pct: Optional[float] = None


@dataclass
class ExitContext:
    """Extra context needed for enhanced exit rules.

    All fields are optional. Missing fields cause the corresponding rule
    to be skipped (never a false positive).
    """
    entry_atm_iv: Optional[float] = None           # ATM IV at position open (absolute, e.g. 0.85 for 85%)
    current_atm_iv: Optional[float] = None         # ATM IV now
    entry_underlying: Optional[float] = None       # underlying price at open
    current_underlying: Optional[float] = None     # underlying price now
    peak_unrealized_pnl: Optional[float] = None    # max unrealized pnl observed during position life
    current_unrealized_pnl: Optional[float] = None # current unrealized pnl
    short_strike: Optional[float] = None           # nearest-to-money short strike (credit trades)
    combo_bid: Optional[float] = None              # combo bid (for spread-defense)
    combo_ask: Optional[float] = None              # combo ask


def evaluate_exit(
    open_debit: float,            # positive for debit, positive for credit (caller passes abs)
    current_mark: float,          # current spread mark
    expiry_date: date,
    today: date,
    rules: StopRules,
    context: Optional[ExitContext] = None,
) -> ExitDecision:
    """Decide whether to exit a position right now.

    Base behavior (no context) is the same as the original implementation.
    When `context` is provided, enhanced rules run in priority order:
      1. Time stop (unconditional)
      2. Short-strike tested (urgent defensive close)
      3. Vol-crush early take-profit
      4. Trailing stop
      5. Base target/stop
      6. Wide-spread DEFER override (can only suppress a target exit)
    """
    dte = (expiry_date - today).days

    # ── 1. Time stop — unconditional ──────────────────────────────────
    if dte <= rules.time_stop_dte:
        return ExitDecision(
            True, "time_stop",
            urgency="urgent" if dte == 0 else "normal",
            detail=f"DTE={dte} ≤ {rules.time_stop_dte}",
        )

    # ── 2. Short-strike tested (credit only, urgent) ──────────────────
    if (
        rules.is_credit
        and rules.short_strike_buffer_pct is not None
        and context is not None
        and context.short_strike is not None
        and context.current_underlying is not None
        and context.short_strike > 0
    ):
        dist_pct = abs(context.current_underlying - context.short_strike) / context.short_strike
        if dist_pct <= rules.short_strike_buffer_pct:
            return ExitDecision(
                True, "strike_tested", "urgent",
                detail=(
                    f"underlying {context.current_underlying:.2f} within "
                    f"{dist_pct*100:.2f}% of short {context.short_strike:.2f}"
                ),
            )

    # ── 3. Vol-crush early take-profit (credit only) ──────────────────
    if (
        rules.is_credit
        and rules.vol_crush_exit_pts is not None
        and context is not None
        and context.entry_atm_iv is not None
        and context.current_atm_iv is not None
    ):
        iv_drop_pts = (context.entry_atm_iv - context.current_atm_iv) * 100.0
        if iv_drop_pts >= rules.vol_crush_exit_pts:
            # Only harvest if the position is actually profitable
            if (
                context.current_unrealized_pnl is not None
                and context.current_unrealized_pnl > 0
            ):
                return ExitDecision(
                    True, "vol_crush", "normal",
                    detail=(
                        f"IV dropped {iv_drop_pts:.1f}pts "
                        f"(entry {context.entry_atm_iv*100:.0f}% → now {context.current_atm_iv*100:.0f}%)"
                    ),
                )

    # ── 4. Trailing stop ──────────────────────────────────────────────
    if (
        rules.trail_activate_pct is not None
        and rules.trail_giveback_pct is not None
        and context is not None
        and context.peak_unrealized_pnl is not None
        and context.current_unrealized_pnl is not None
    ):
        # Rough proxy: max realizable profit ≈ abs(open_debit) * mult.
        # This works for both credit (credit × mult) and debit (wing_width - debit) × mult
        # as a ballpark. The trailing rule is about locking in gains, not about
        # being exact — a proxy is fine.
        max_profit_proxy = abs(open_debit) * 1000.0
        activation = rules.trail_activate_pct * max_profit_proxy
        if (
            max_profit_proxy > 0
            and context.peak_unrealized_pnl >= activation
        ):
            giveback = context.peak_unrealized_pnl - context.current_unrealized_pnl
            threshold = rules.trail_giveback_pct * context.peak_unrealized_pnl
            if giveback >= threshold and giveback > 0:
                return ExitDecision(
                    True, "trail", "normal",
                    detail=(
                        f"gave back ${giveback:.0f} from peak ${context.peak_unrealized_pnl:.0f} "
                        f"(≥ {rules.trail_giveback_pct*100:.0f}%)"
                    ),
                )

    # ── 5. Base target / stop ─────────────────────────────────────────
    decision: ExitDecision
    if rules.is_credit:
        if current_mark <= open_debit * (1.0 - rules.profit_target_pct):
            decision = ExitDecision(True, "target", "normal")
        elif current_mark >= open_debit * (1.0 + rules.stop_loss_pct):
            decision = ExitDecision(True, "stop", "urgent")
        else:
            return ExitDecision(False, "hold")
    else:
        if current_mark >= open_debit * (1.0 + rules.profit_target_pct):
            decision = ExitDecision(True, "target", "normal")
        elif current_mark <= open_debit * (1.0 - rules.stop_loss_pct):
            decision = ExitDecision(True, "stop", "urgent")
        else:
            return ExitDecision(False, "hold")

    # ── 6. Wide-spread defense — can only DEFER a target, never a stop ──
    if (
        decision.reason == "target"
        and rules.min_combo_spread_pct is not None
        and context is not None
        and context.combo_bid is not None
        and context.combo_ask is not None
    ):
        mid = (context.combo_bid + context.combo_ask) / 2.0
        if abs(mid) > 0.01:
            spread_width = abs(context.combo_ask - context.combo_bid)
            spread_pct = spread_width / abs(mid)
            if spread_pct > rules.min_combo_spread_pct:
                return ExitDecision(
                    False, "defer",
                    detail=(
                        f"wide spread ({spread_width:.3f} = "
                        f"{spread_pct*100:.0f}% of mid); deferring target exit"
                    ),
                )

    return decision


def gap_risk_buffer(
    underlying_realized_vol_annual: float,
    underlying_price: float,
    days_to_expiry: int,
) -> float:
    """Estimate the dollar size of a 2-stdev overnight gap.

    Used to add a margin to stop-loss thresholds for positions held overnight.
    """
    daily_vol = underlying_realized_vol_annual / (252 ** 0.5)
    one_stdev = underlying_price * daily_vol
    return 2.0 * one_stdev


def is_position_overnight_eligible(
    days_to_expiry: int,
    realized_vol_annual: float,
    threshold_vol: float = 1.0,
) -> bool:
    """Refuse to hold positions with < 5 DTE through overnight if vol is extreme."""
    if days_to_expiry <= 5 and realized_vol_annual > threshold_vol:
        return False
    return True
