"""Bull put credit spread.

Sells a put closer to the money (~25Δ), buys a further-OTM put as wing.
Net credit. Bullish bias: profits if underlying stays above the short strike
at expiry.

Use when:
  - Vol is rich (we're a net premium seller)
  - Recent flush has overshot to the downside (mean reversion bull)
  - Option chain shows put-skew (rich downside wings)
"""

from __future__ import annotations

from core.contracts import FopSpec, ComboLegSpec
from core.risk import StopRules
from core.verticals import find_credit_vertical, is_mean_reversion_long_setup
from strategies.base import Strategy, StrategySignal


class BullPutCredit(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 0.50)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 1.50)),
            time_stop_dte=int(self.params.get("time_stop_dte", 3)),
            is_credit=True,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        if not self.enabled or not self.can_open_more(current_positions):
            return []

        snap = market_state.get("snapshot")
        if snap is None or snap.empty:
            return []

        if not is_mean_reversion_long_setup(snap):
            return []

        candidate = find_credit_vertical(
            snap,
            trading_class=self.params.get("trading_class", "LO"),
            right="P",
            target_short_delta=float(self.params.get("short_put_delta", -0.25)),
            width=float(self.params.get("wing_offset", 5)),
            target_dte_min=int(self.params.get("target_dte_min", 7)),
            target_dte_max=int(self.params.get("target_dte_max", 21)),
            min_iv=float(self.params.get("vol_filter_min_iv", 0.40)),
            min_credit_pct_of_width=float(self.params.get("min_credit_pct_of_width", 0.20)),
        )
        if candidate is None:
            return []

        qty = int(self.params.get("default_qty", 1))
        legs = [
            ComboLegSpec(FopSpec("CL", candidate.trading_class, candidate.expiry, candidate.short_strike, "P"), qty, "SELL"),
            ComboLegSpec(FopSpec("CL", candidate.trading_class, candidate.expiry, candidate.long_strike, "P"), qty, "BUY"),
        ]
        thesis = (
            f"BullPutCredit: ATM IV {candidate.atm_iv*100:.1f}% on {candidate.trading_class} {candidate.expiry}, "
            f"sell {candidate.short_strike:.0f}P, buy {candidate.long_strike:.0f}P wing "
            f"(width=${candidate.width:.0f}, credit=${-candidate.net_amount:.2f}). "
            f"DTE={candidate.dte}. Spot={candidate.spot:.2f}. Profits if CL stays above breakeven."
        )
        return [StrategySignal(
            structure="bull_put_credit_spread",
            legs=legs,
            qty=qty,
            target_debit=candidate.net_amount,
            max_loss=candidate.max_loss,
            max_profit=candidate.max_profit,
            expected_value=None,
            expiry_date=candidate.expiry_date,
            thesis=thesis,
            confidence=0.55,
            metadata={"atm_iv": candidate.atm_iv, "dte": candidate.dte, "spot": candidate.spot},
        )]
