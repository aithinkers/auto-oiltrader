"""Post-event volatility crush strategy.

Sells short premium after a binary geopolitical event resolves and IV drops.
STUB — to be implemented in Phase 2.
"""

from __future__ import annotations

from core.risk import StopRules
from strategies.base import Strategy, StrategySignal


class VolCrushPostEvent(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 0.30)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 1.50)),
            time_stop_dte=int(self.params.get("time_stop_dte", 5)),
            is_credit=True,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        # TODO Phase 2: detect IV drops > threshold and emit short strangle signals
        return []
