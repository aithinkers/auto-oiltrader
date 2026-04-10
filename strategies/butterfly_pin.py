"""Long butterfly targeting a specific pin price (e.g., RND median, OI magnet).

STUB — to be implemented in Phase 2 after iron_condor_range is validated.
"""

from __future__ import annotations

from core.risk import StopRules
from strategies.base import Strategy, StrategySignal


class ButterflyPin(Strategy):

    def _build_stop_rules(self) -> StopRules:
        return StopRules(
            profit_target_pct=float(self.params.get("profit_target_pct", 2.0)),
            stop_loss_pct=float(self.params.get("stop_loss_pct", 0.50)),
            time_stop_dte=int(self.params.get("time_stop_dte", 2)),
            is_credit=False,
        )

    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        # TODO Phase 2: implement based on RND median or OI-magnet strike detection
        return []
