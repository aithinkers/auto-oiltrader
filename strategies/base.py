"""Base classes for trading strategies.

A strategy:
  1. Reads market state (snapshot, positions, news)
  2. Decides whether to propose a new trade (Recommendation)
  3. Has a `tier` (experimental | shadow | paper | draft | live) that gates execution
  4. Has stop/target rules used by the position manager

Strategies do NOT place orders directly. They emit Recommendations into the DB,
and the trader_agent decides whether to convert them to orders based on tier and mode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from core.contracts import ComboLegSpec
from core.risk import StopRules


@dataclass
class StrategySignal:
    """A proposed trade. Becomes a Recommendation row when emitted."""
    structure: str                       # 'iron_condor' | 'butterfly' | etc
    legs: list[ComboLegSpec]
    qty: int
    target_debit: float                  # positive = pay, negative = collect
    max_loss: float                      # always positive
    max_profit: float                    # always positive
    expected_value: float | None
    expiry_date: date
    thesis: str
    confidence: float                    # 0..1
    metadata: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Abstract base for all rules-based strategies."""

    id: str
    name: str
    tier: str
    enabled: bool
    params: dict[str, Any]
    stop_rules: StopRules

    def __init__(
        self,
        id: str,
        name: str,
        tier: str,
        enabled: bool,
        params: dict[str, Any],
    ) -> None:
        self.id = id
        self.name = name
        self.tier = tier
        self.enabled = enabled
        self.params = params
        self.stop_rules = self._build_stop_rules()

    @abstractmethod
    def _build_stop_rules(self) -> StopRules:
        """Construct stop/target rules from params dict."""
        ...

    @abstractmethod
    def evaluate(self, market_state: dict, current_positions: list[dict]) -> list[StrategySignal]:
        """Look at the market state and return a list of new trade signals.

        `market_state` includes:
          - 'snapshot': latest pandas df from the stream
          - 'spot': dict of futures last prices keyed by local_symbol
          - 'iv_surface': dict of (class, expiry) -> ATM IV
          - 'news': recent high-impact news items
          - 'time': current datetime

        `current_positions` is the list of open positions tagged with this strategy_id.

        Return [] if no new signals.
        """
        ...

    def can_open_more(self, current_positions: list[dict]) -> bool:
        """Default: respect max_concurrent param."""
        max_concurrent = self.params.get("max_concurrent", 1)
        return len(current_positions) < max_concurrent
