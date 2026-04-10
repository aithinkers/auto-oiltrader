"""Rolling window of active futures contracts.

CL futures expire monthly. The system tracks the next N months of futures and
their associated option chains. As contracts approach expiry, they are dropped
from the active set and replaced by the next forward month.

This module is the single source of truth for "what should be subscribed/traded
right now." All daemons read from here:

  - stream_daemon: which futures + LO chains to subscribe to
  - trader_daemon: which contracts can have NEW orders opened against them
  - position_manager: continues to mark dropped contracts until positions close
  - reconciler: warns if IB has positions in non-active contracts

The window is recomputed at startup and again daily at `rebuild_hour_et`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable


@dataclass(frozen=True)
class ContractInfo:
    """Light-weight description of a futures contract.

    Filled in from IB contractDetails. The system uses local_symbol (e.g. 'CLK6')
    as the canonical identifier; conId is for IB API calls.
    """
    symbol: str          # 'CL', 'BZ'
    local_symbol: str    # 'CLK6'
    expiry: str          # 'YYYYMMDD'
    con_id: int
    exchange: str = "NYMEX"

    @property
    def expiry_date(self) -> date:
        return datetime.strptime(self.expiry, "%Y%m%d").date()

    def dte(self, today: date | None = None) -> int:
        return (self.expiry_date - (today or date.today())).days


@dataclass
class ActiveWindow:
    """The current rolling window of active contracts.

    `tradeable` are contracts open for new positions.
    `markable` are contracts the position manager still marks (existing positions
    still open on contracts that have aged past the drop threshold).
    """
    tradeable: list[ContractInfo]
    markable: list[ContractInfo]
    computed_at: datetime
    n_months_ahead: int
    drop_when_dte_le: int

    def is_tradeable(self, local_symbol: str) -> bool:
        return any(c.local_symbol == local_symbol for c in self.tradeable)

    def is_markable(self, local_symbol: str) -> bool:
        return any(c.local_symbol == local_symbol for c in self.tradeable + self.markable)

    def front_month(self) -> ContractInfo | None:
        if not self.tradeable:
            return None
        return min(self.tradeable, key=lambda c: c.expiry_date)


def compute_active_window(
    discovered_contracts: Iterable[ContractInfo],
    open_position_locals: set[str],
    n_months_ahead: int,
    drop_when_dte_le: int,
    today: date | None = None,
) -> ActiveWindow:
    """Compute which contracts should be tradeable and which only markable.

    Inputs:
      - discovered_contracts: every CL future the IB API returned (sorted or not)
      - open_position_locals: local_symbols where we currently have open positions
      - n_months_ahead: rolling window depth (e.g., 3)
      - drop_when_dte_le: drop from tradeable if DTE ≤ this

    Returns:
      ActiveWindow with disjoint `tradeable` and `markable` lists.

    Logic:
      1. Sort discovered contracts by expiry date.
      2. Drop any contracts already past expiry.
      3. Apply DTE filter: contracts with DTE > drop_when_dte_le are eligible
         for the tradeable window.
      4. Take the first `n_months_ahead` eligible contracts → tradeable.
      5. Any contract with DTE ≤ drop_when_dte_le AND we have open positions on it
         goes to `markable` so the position manager keeps marking it.
      6. Anything past expiry, or with no positions and below drop threshold,
         is dropped entirely.
    """
    today = today or date.today()
    sorted_contracts = sorted(discovered_contracts, key=lambda c: c.expiry_date)

    # Drop expired contracts entirely
    not_expired = [c for c in sorted_contracts if c.expiry_date >= today]

    tradeable: list[ContractInfo] = []
    markable: list[ContractInfo] = []

    for c in not_expired:
        dte = c.dte(today)
        if dte > drop_when_dte_le and len(tradeable) < n_months_ahead:
            tradeable.append(c)
        elif c.local_symbol in open_position_locals:
            # We still have a position on this contract; keep marking it
            markable.append(c)
        # else: drop entirely (no positions, too close to expiry)

    return ActiveWindow(
        tradeable=tradeable,
        markable=markable,
        computed_at=datetime.now(),
        n_months_ahead=n_months_ahead,
        drop_when_dte_le=drop_when_dte_le,
    )


def needs_rebuild(window: ActiveWindow | None, now: datetime, rebuild_hour_et: int) -> bool:
    """Decide whether the window should be recomputed right now.

    Returns True if:
      - There is no current window
      - Front month has rolled past the drop threshold since last computation
      - We've crossed the daily rebuild hour and haven't recomputed yet today
    """
    if window is None:
        return True

    today = now.date()
    if window.computed_at.date() < today and now.hour >= rebuild_hour_et:
        return True

    front = window.front_month()
    if front and front.dte(today) <= window.drop_when_dte_le:
        return True

    return False


def diff_windows(old: ActiveWindow | None, new: ActiveWindow) -> dict:
    """Return added/removed local_symbols between two windows.

    Used by stream_daemon to know which subscriptions to add/cancel,
    and by the narrator to emit a commentary line on roll events.
    """
    old_set = set(c.local_symbol for c in (old.tradeable if old else []))
    new_set = set(c.local_symbol for c in new.tradeable)
    return {
        "added": sorted(new_set - old_set),
        "removed": sorted(old_set - new_set),
        "unchanged": sorted(old_set & new_set),
    }
