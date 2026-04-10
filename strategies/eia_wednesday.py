"""EIA Wednesday vol expansion play.

Buys a strangle Tuesday afternoon, exits Wednesday after the EIA report.
STUB — to be implemented in Phase 3.
"""

from __future__ import annotations

from core.risk import StopRules
from strategies.base import Strategy, StrategySignal


class EIAWednesday(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 1.00)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 0.50)),
            time_stop_dte=1,
            is_credit=False,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        # TODO Phase 3: implement schedule-based entry on Tuesday, exit Wednesday
        return []
