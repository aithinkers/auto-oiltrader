"""Shared helpers for building vertical spread strategies.

A vertical spread is a 2-leg structure: long one strike and short another at the
same expiry, both same right (calls or puts). All four directional spreads share
the same chain-walking + delta-targeting + sizing math, so we put it here.

Used by:
  - bull_put_credit_spread (sell higher-K put, buy lower-K put)  → bullish credit
  - bear_call_credit_spread (sell lower-K call, buy higher-K call) → bearish credit
  - bull_call_debit_spread (buy lower-K call, sell higher-K call) → bullish debit
  - bear_put_debit_spread (buy higher-K put, sell lower-K put) → bearish debit
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd


@dataclass
class VerticalCandidate:
    """A vetted vertical-spread candidate ready to become a StrategySignal."""
    trading_class: str
    expiry: str
    expiry_date: date
    dte: int
    long_strike: float
    short_strike: float
    right: str           # 'C' or 'P'
    long_mid: float
    short_mid: float
    width: float
    net_amount: float    # positive = debit, negative = credit
    max_profit: float    # in dollars per 1 unit
    max_loss: float      # in dollars per 1 unit
    atm_iv: float
    spot: float


def _eligible_chain(snap: pd.DataFrame, trading_class: str) -> pd.DataFrame:
    """Filter snapshot to FOPs we can use: have IV, mid, delta."""
    if snap.empty:
        return snap
    sub = snap[
        (snap["sec_type"] == "FOP")
        & (snap["trading_class"] == trading_class)
        & (snap["iv"] > 0)
        & (snap["delta"].notna())
        & (snap["mid"].notna())
        & (snap["mid"] > 0)
        & (snap["underlying_last"].notna())
    ]
    return sub


def _expiry_groups(chain: pd.DataFrame, target_dte_min: int, target_dte_max: int, today: date) -> list[tuple[str, date, int, pd.DataFrame]]:
    """Return [(expiry, expiry_date, dte, sub_df), ...] within DTE window."""
    if chain.empty:
        return []
    out: list[tuple[str, date, int, pd.DataFrame]] = []
    for expiry, sub in chain.groupby("expiry"):
        try:
            exp_dt = datetime.strptime(str(expiry), "%Y%m%d").date()
        except (ValueError, TypeError):
            continue
        dte = (exp_dt - today).days
        if target_dte_min <= dte <= target_dte_max:
            out.append((str(expiry), exp_dt, dte, sub))
    out.sort(key=lambda t: t[2])
    return out


def _atm_iv(sub: pd.DataFrame, spot: float) -> float:
    """Mean of nearest call+put IVs to spot."""
    sub = sub.copy()
    sub["dist"] = (sub["strike"] - spot).abs()
    closest = sub.sort_values("dist").head(2)
    return float(closest["iv"].mean())


def find_credit_vertical(
    snap: pd.DataFrame,
    *,
    trading_class: str,
    right: str,                  # 'P' for bull put credit, 'C' for bear call credit
    target_short_delta: float,   # signed: -0.20 for puts, +0.20 for calls
    width: float,
    target_dte_min: int,
    target_dte_max: int,
    min_iv: float,
    min_credit_pct_of_width: float,
    today: Optional[date] = None,
) -> Optional[VerticalCandidate]:
    """Find one credit-vertical candidate matching the criteria.

    Returns the FIRST eligible (class, expiry) match. Returns None if no eligible
    candidate exists. Use a directional pre-filter (e.g. mean reversion) BEFORE
    calling this — this function only validates the structural fit.
    """
    today = today or date.today()
    chain = _eligible_chain(snap, trading_class)
    if chain.empty:
        return None

    for expiry, exp_dt, dte, sub in _expiry_groups(chain, target_dte_min, target_dte_max, today):
        spot = float(sub["underlying_last"].iloc[0])
        atm_iv = _atm_iv(sub, spot)
        if atm_iv < min_iv:
            continue

        side = sub[sub["right"] == right]
        if len(side) < 4:
            continue

        # Find short strike closest to target delta
        side = side.copy()
        side["delta_diff"] = (side["delta"] - target_short_delta).abs()
        short_row = side.sort_values("delta_diff").iloc[0]
        short_k = float(short_row["strike"])

        # Long wing offset
        if right == "P":
            long_k = short_k - width
        else:
            long_k = short_k + width

        long_row = side[side["strike"] == long_k]
        if long_row.empty:
            # No exact match — try the nearest available
            side["k_diff"] = (side["strike"] - long_k).abs()
            long_row = side.sort_values("k_diff").head(1)
            if long_row.empty:
                continue
            long_k = float(long_row["strike"].iloc[0])

        long_row = long_row.iloc[0] if hasattr(long_row, "iloc") else long_row
        short_mid = float(short_row["mid"])
        long_mid = float(long_row["mid"])

        credit = short_mid - long_mid
        actual_width = abs(short_k - long_k)
        if actual_width <= 0 or credit <= 0:
            continue
        if credit < min_credit_pct_of_width * actual_width:
            continue

        max_profit = credit * 1000
        max_loss = (actual_width - credit) * 1000

        return VerticalCandidate(
            trading_class=trading_class,
            expiry=expiry,
            expiry_date=exp_dt,
            dte=dte,
            long_strike=long_k,
            short_strike=short_k,
            right=right,
            long_mid=long_mid,
            short_mid=short_mid,
            width=actual_width,
            net_amount=-credit,  # negative = credit
            max_profit=max_profit,
            max_loss=max_loss,
            atm_iv=atm_iv,
            spot=spot,
        )

    return None


def find_debit_vertical(
    snap: pd.DataFrame,
    *,
    trading_class: str,
    right: str,                  # 'C' for bull call debit, 'P' for bear put debit
    target_long_delta: float,    # signed: +0.50 for ATM call, -0.50 for ATM put
    width: float,
    target_dte_min: int,
    target_dte_max: int,
    max_iv: float,               # don't pay debit when vol is too rich
    max_debit_pct_of_width: float,
    today: Optional[date] = None,
) -> Optional[VerticalCandidate]:
    """Find one debit-vertical candidate."""
    today = today or date.today()
    chain = _eligible_chain(snap, trading_class)
    if chain.empty:
        return None

    for expiry, exp_dt, dte, sub in _expiry_groups(chain, target_dte_min, target_dte_max, today):
        spot = float(sub["underlying_last"].iloc[0])
        atm_iv = _atm_iv(sub, spot)
        if atm_iv > max_iv:
            continue

        side = sub[sub["right"] == right]
        if len(side) < 4:
            continue

        side = side.copy()
        side["delta_diff"] = (side["delta"] - target_long_delta).abs()
        long_row = side.sort_values("delta_diff").iloc[0]
        long_k = float(long_row["strike"])

        if right == "C":
            short_k = long_k + width
        else:
            short_k = long_k - width

        short_row = side[side["strike"] == short_k]
        if short_row.empty:
            side["k_diff"] = (side["strike"] - short_k).abs()
            short_row = side.sort_values("k_diff").head(1)
            if short_row.empty:
                continue
            short_k = float(short_row["strike"].iloc[0])

        short_row = short_row.iloc[0] if hasattr(short_row, "iloc") else short_row
        long_mid = float(long_row["mid"])
        short_mid = float(short_row["mid"])

        debit = long_mid - short_mid
        actual_width = abs(long_k - short_k)
        if actual_width <= 0 or debit <= 0:
            continue
        if debit > max_debit_pct_of_width * actual_width:
            continue

        max_profit = (actual_width - debit) * 1000
        max_loss = debit * 1000

        return VerticalCandidate(
            trading_class=trading_class,
            expiry=expiry,
            expiry_date=exp_dt,
            dte=dte,
            long_strike=long_k,
            short_strike=short_k,
            right=right,
            long_mid=long_mid,
            short_mid=short_mid,
            width=actual_width,
            net_amount=debit,    # positive = debit
            max_profit=max_profit,
            max_loss=max_loss,
            atm_iv=atm_iv,
            spot=spot,
        )

    return None


# ---------------------------------------------------------------------------
# Mean-reversion directional filter — used by credit spreads to avoid being on
# the wrong side of a strong trend
# ---------------------------------------------------------------------------
def is_mean_reversion_long_setup(snap: pd.DataFrame, lookback_pct: float = 0.05) -> bool:
    """Returns True if the front future is in a state where a bullish credit
    spread (sell put) has favorable conditions.

    Phase 1 heuristic: requires that we know recent realized vol > 50%, which
    proxies a recent flush. A real implementation would compare current spot
    to a rolling window low; we'll add that when historical bars are wired in.
    For now this is a stub that returns True so the strategy can fire on its
    own structural filters (delta + IV + credit).
    """
    return True


def is_mean_reversion_short_setup(snap: pd.DataFrame, lookback_pct: float = 0.05) -> bool:
    """Mirror of long setup — for bearish credit spreads."""
    return True
