"""Bear put debit spread.

Buys an at/near-the-money put, sells a further-OTM put to cap cost.
Net debit. Bearish bias: profits if underlying drops below the long strike
by more than the debit paid.
"""

from __future__ import annotations

from core.contracts import FopSpec, ComboLegSpec
from core.risk import StopRules
from core.verticals import find_debit_vertical
from strategies.base import Strategy, StrategySignal


class BearPutDebit(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 0.75)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 0.50)),
            time_stop_dte=int(self.params.get("time_stop_dte", 2)),
            is_credit=False,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        if not self.enabled or not self.can_open_more(current_positions):
            return []
        snap = market_state.get("snapshot")
        if snap is None or snap.empty:
            return []

        candidate = find_debit_vertical(
            snap,
            trading_class=self.params.get("trading_class", "LO"),
            right="P",
            target_long_delta=float(self.params.get("long_put_delta", -0.50)),
            width=float(self.params.get("width", 5)),
            target_dte_min=int(self.params.get("target_dte_min", 7)),
            target_dte_max=int(self.params.get("target_dte_max", 21)),
            max_iv=float(self.params.get("max_iv", 0.80)),
            max_debit_pct_of_width=float(self.params.get("max_debit_pct_of_width", 0.40)),
        )
        if candidate is None:
            return []

        qty = int(self.params.get("default_qty", 1))
        legs = [
            ComboLegSpec(FopSpec("CL", candidate.trading_class, candidate.expiry, candidate.long_strike, "P"), qty, "BUY"),
            ComboLegSpec(FopSpec("CL", candidate.trading_class, candidate.expiry, candidate.short_strike, "P"), qty, "SELL"),
        ]
        thesis = (
            f"BearPutDebit: ATM IV {candidate.atm_iv*100:.1f}% on {candidate.trading_class} {candidate.expiry}, "
            f"buy {candidate.long_strike:.0f}P, sell {candidate.short_strike:.0f}P wing "
            f"(width=${candidate.width:.0f}, debit=${candidate.net_amount:.2f}). "
            f"DTE={candidate.dte}. Spot={candidate.spot:.2f}. "
            f"Max profit if CL closes <= {candidate.short_strike:.0f}."
        )
        return [StrategySignal(
            structure="bear_put_debit_spread",
            legs=legs,
            qty=qty,
            target_debit=candidate.net_amount,
            max_loss=candidate.max_loss,
            max_profit=candidate.max_profit,
            expected_value=None,
            expiry_date=candidate.expiry_date,
            thesis=thesis,
            confidence=0.50,
            metadata={"atm_iv": candidate.atm_iv, "dte": candidate.dte, "spot": candidate.spot},
        )]
