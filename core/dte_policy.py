"""DTE policy — when is it OK to open a new position close to expiry?

Front-month options have the highest theta AND the highest gamma. They are the
sweet spot for some strategies (pin trades, vol crush plays, hedges) and
suicidal for others (selling premium into a fast-moving market).

This module is the single source of truth for "what's the minimum DTE allowed
for THIS proposed trade in THIS market state?". The trade agent calls
`min_dte_for_new_position()` before placing any order.

The decision walks through the scenarios in priority order. The most
permissive matching scenario wins. If nothing matches, the default floor
applies. Hard-NO conditions override everything else and return BLOCK_ALL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Scenario(str, Enum):
    """The scenario that decided the floor. Stored on the recommendation row."""
    DEFAULT = "default"
    PREMIUM_SELL_CALM = "premium_sell_calm"
    PREMIUM_SELL_NORMAL = "premium_sell_normal"
    PREMIUM_SELL_HIGH_VOL = "premium_sell_high_vol"
    LONG_DEBIT_NORMAL = "long_debit_normal"
    LONG_DEBIT_HIGH_IV = "long_debit_high_iv"
    LONG_DEBIT_WIDE_SPREAD = "long_debit_wide_spread"
    PIN_TRADE = "pin_trade"
    PIN_TRADE_STRONG_MAGNET = "pin_trade_strong_magnet"
    HEDGE = "hedge"
    SCHEDULED_EVENT = "scheduled_event"
    BLOCKED = "blocked"


# Sentinel — bigger than any reasonable expiry, signals "do not allow this trade"
BLOCK_ALL = 9999


@dataclass
class DTEPolicyConfig:
    """Loaded from config/settings.toml [dte_policy]."""
    default_min_dte: int = 10
    premium_sell_min_dte_calm: int = 5
    premium_sell_min_dte_normal: int = 10
    premium_sell_min_dte_high_vol: int = 14
    long_debit_min_dte_normal: int = 5
    long_debit_min_dte_high_iv: int = 7
    long_debit_min_dte_wide_spread: int = 10
    pin_trade_min_dte_normal: int = 5
    pin_trade_min_dte_strong_magnet: int = 3
    hedge_min_dte: int = 1
    hedge_max_pct_of_book: float = 0.50
    event_trade_min_dte: int = 1
    event_trade_max_dte: int = 14
    refuse_dte_zero: bool = True
    refuse_when_realized_vol_above: float = 1.0
    refuse_when_combo_spread_pct_above: float = 0.25
    refuse_when_iv_change_60min_above: float = 3.0
    fast_move_iv_change_window_min: int = 60
    recent_news_critical_window_hours: int = 6
    recent_news_critical_block_hours: int = 1
    recent_news_short_dte_block_hours: int = 24


@dataclass
class MarketState:
    """Snapshot of market conditions used by the policy.

    All fields are optional; missing data → conservative interpretation.
    """
    atm_iv: float | None = None              # 0..2 (e.g., 0.95 = 95%)
    realized_vol_10d: float | None = None    # 0..2
    iv_change_60min_pts: float | None = None # vol points (1 = 1%)
    combo_bid_ask_pct: float | None = None   # bid-ask as fraction of mid
    has_critical_news_within_hours: int | None = None  # hours since latest critical, None if no critical
    has_scheduled_event_within_hours: int | None = None
    is_fast_market: bool = False             # explicit override
    proposed_dte: int = 0


@dataclass
class TradeContext:
    """What the proposed trade looks like, used to pick the right scenario."""
    structure: str                  # 'iron_condor' | 'butterfly' | 'put_spread' | etc.
    is_credit: bool                 # True for premium-collection structures
    is_hedge: bool = False          # True if reducing book risk on existing position
    is_pin_trade: bool = False      # True if structure is butterfly aimed at OI magnet
    has_strong_oi_magnet: bool = False
    is_scheduled_event_play: bool = False
    proposed_size_pct_of_book: float = 0.0


@dataclass
class DTEDecision:
    """Result of the policy walker."""
    min_dte: int
    scenario: Scenario
    reason: str
    blocking: bool                  # True if BLOCK_ALL — refuse the trade entirely

    @property
    def allows(self) -> bool:
        """Whether the policy permits a trade at the proposed DTE."""
        return not self.blocking


def min_dte_for_new_position(
    ctx: TradeContext,
    state: MarketState,
    cfg: DTEPolicyConfig,
) -> DTEDecision:
    """Walk the scenarios and return the most permissive applicable DTE floor.

    The trade agent uses this as a hard floor: if `state.proposed_dte` is
    less than the returned `min_dte`, the trade is rejected.
    """

    # ----- HARD-NO CONDITIONS -----
    # Checked first; any one trips → block_all
    blocks = _check_hard_no(ctx, state, cfg)
    if blocks:
        return DTEDecision(
            min_dte=BLOCK_ALL,
            scenario=Scenario.BLOCKED,
            reason="; ".join(blocks),
            blocking=True,
        )

    # ----- HEDGE: most permissive — always wins if applicable -----
    if ctx.is_hedge:
        if ctx.proposed_size_pct_of_book > cfg.hedge_max_pct_of_book:
            return DTEDecision(
                min_dte=BLOCK_ALL,
                scenario=Scenario.BLOCKED,
                reason=f"Hedge size {ctx.proposed_size_pct_of_book:.0%} > "
                       f"hedge_max_pct_of_book {cfg.hedge_max_pct_of_book:.0%}; "
                       f"requires human approval",
                blocking=True,
            )
        return DTEDecision(
            min_dte=cfg.hedge_min_dte,
            scenario=Scenario.HEDGE,
            reason=f"Hedge for existing position; floor lifted to {cfg.hedge_min_dte} DTE",
            blocking=False,
        )

    # ----- SCHEDULED EVENT PLAY -----
    if ctx.is_scheduled_event_play:
        return DTEDecision(
            min_dte=cfg.event_trade_min_dte,
            scenario=Scenario.SCHEDULED_EVENT,
            reason=f"Scheduled-event trade; floor {cfg.event_trade_min_dte} DTE, "
                   f"max {cfg.event_trade_max_dte} DTE",
            blocking=False,
        )

    # ----- PIN TRADE (butterfly at OI magnet) -----
    if ctx.is_pin_trade:
        if ctx.has_strong_oi_magnet:
            return DTEDecision(
                min_dte=cfg.pin_trade_min_dte_strong_magnet,
                scenario=Scenario.PIN_TRADE_STRONG_MAGNET,
                reason=f"Pin trade with strong OI magnet; floor {cfg.pin_trade_min_dte_strong_magnet} DTE",
                blocking=False,
            )
        return DTEDecision(
            min_dte=cfg.pin_trade_min_dte_normal,
            scenario=Scenario.PIN_TRADE,
            reason=f"Pin trade; floor {cfg.pin_trade_min_dte_normal} DTE",
            blocking=False,
        )

    # ----- PREMIUM-SELL SCENARIOS -----
    if ctx.is_credit:
        iv = state.atm_iv
        rv = state.realized_vol_10d

        # Recent critical news → conservative floor regardless
        if state.has_critical_news_within_hours is not None and state.has_critical_news_within_hours <= cfg.recent_news_short_dte_block_hours:
            return DTEDecision(
                min_dte=cfg.premium_sell_min_dte_high_vol + 4,  # extra cushion
                scenario=Scenario.PREMIUM_SELL_HIGH_VOL,
                reason=f"Critical news in last {cfg.recent_news_short_dte_block_hours}h "
                       f"→ extended DTE floor for credit trades",
                blocking=False,
            )

        # IV>80 + RV>50 → high-vol regime, refuse short DTE
        if iv is not None and iv > 0.80 and rv is not None and rv > 0.50:
            return DTEDecision(
                min_dte=cfg.premium_sell_min_dte_high_vol,
                scenario=Scenario.PREMIUM_SELL_HIGH_VOL,
                reason=f"IV {iv*100:.0f}% > 80% AND RV {rv*100:.0f}% > 50% — "
                       f"gamma risk too high for short premium close to expiry",
                blocking=False,
            )

        # Sweet spot: rich vol but calm realized
        if iv is not None and iv > 0.50 and rv is not None and rv < 0.35:
            return DTEDecision(
                min_dte=cfg.premium_sell_min_dte_calm,
                scenario=Scenario.PREMIUM_SELL_CALM,
                reason=f"IV {iv*100:.0f}% rich, RV {rv*100:.0f}% calm — "
                       f"sweet spot for short premium",
                blocking=False,
            )

        # Low IV — not enough premium to justify the risk
        if iv is not None and iv < 0.30:
            return DTEDecision(
                min_dte=cfg.premium_sell_min_dte_normal,
                scenario=Scenario.PREMIUM_SELL_NORMAL,
                reason=f"IV {iv*100:.0f}% < 30% — too cheap to sell, "
                       f"keeping standard floor",
                blocking=False,
            )

        # Default for credit trades
        return DTEDecision(
            min_dte=cfg.premium_sell_min_dte_normal,
            scenario=Scenario.PREMIUM_SELL_NORMAL,
            reason=f"Standard credit-trade DTE floor",
            blocking=False,
        )

    # ----- LONG DEBIT SCENARIOS -----
    iv = state.atm_iv

    if state.combo_bid_ask_pct is not None and state.combo_bid_ask_pct > 0.10:
        return DTEDecision(
            min_dte=cfg.long_debit_min_dte_wide_spread,
            scenario=Scenario.LONG_DEBIT_WIDE_SPREAD,
            reason=f"Combo bid/ask is {state.combo_bid_ask_pct:.0%} of mid — "
                   f"slippage risk requires longer DTE",
            blocking=False,
        )

    if iv is not None and iv > 0.80:
        return DTEDecision(
            min_dte=cfg.long_debit_min_dte_high_iv,
            scenario=Scenario.LONG_DEBIT_HIGH_IV,
            reason=f"IV {iv*100:.0f}% > 80% — vol crush risk on long premium "
                   f"requires longer DTE",
            blocking=False,
        )

    return DTEDecision(
        min_dte=cfg.long_debit_min_dte_normal,
        scenario=Scenario.LONG_DEBIT_NORMAL,
        reason=f"Standard long-debit DTE floor",
        blocking=False,
    )


def _check_hard_no(
    ctx: TradeContext,
    state: MarketState,
    cfg: DTEPolicyConfig,
) -> list[str]:
    """Return a list of hard-NO reasons. Empty list = no hard blocks."""
    blocks: list[str] = []

    if cfg.refuse_dte_zero and state.proposed_dte <= 0:
        blocks.append("DTE = 0 is dealer territory; we don't have edge")

    if (
        state.realized_vol_10d is not None
        and state.realized_vol_10d > cfg.refuse_when_realized_vol_above
    ):
        blocks.append(
            f"Realized vol {state.realized_vol_10d*100:.0f}% > "
            f"{cfg.refuse_when_realized_vol_above*100:.0f}% — daily moves too violent"
        )

    if (
        state.combo_bid_ask_pct is not None
        and state.combo_bid_ask_pct > cfg.refuse_when_combo_spread_pct_above
    ):
        blocks.append(
            f"Combo bid/ask {state.combo_bid_ask_pct:.0%} > "
            f"{cfg.refuse_when_combo_spread_pct_above:.0%} — slippage ≈ entire edge"
        )

    if (
        state.iv_change_60min_pts is not None
        and abs(state.iv_change_60min_pts) > cfg.refuse_when_iv_change_60min_above
    ):
        blocks.append(
            f"IV change {state.iv_change_60min_pts:+.1f} vol pts in last 60 min — "
            f"fast-move state, quotes are stale"
        )

    if state.is_fast_market:
        blocks.append("Fast-market state explicitly flagged")

    if (
        state.has_critical_news_within_hours is not None
        and state.has_critical_news_within_hours <= cfg.recent_news_critical_block_hours
    ):
        blocks.append(
            f"Critical news within last {cfg.recent_news_critical_block_hours}h — "
            f"blanket refusal until digestion"
        )

    return blocks
