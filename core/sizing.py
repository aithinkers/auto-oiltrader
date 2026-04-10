"""Position sizing and capital boundary enforcement.

The single source of truth for "is this trade allowed?". Every order must
pass `check_proposed_order` before submission, regardless of mode.

Limits are loaded from `config/settings.toml` `[capital]` section. The
loader is `load_limits_from_settings()`. Daemons should call this once
at startup and pass the resulting `CapitalLimits` to every check.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from core.db import get_conn


@dataclass
class SizeCheckResult:
    allowed: bool
    reason: str
    proposed_max_loss: float
    new_total_book_risk: float
    capital_ceiling: float


@dataclass
class CapitalLimits:
    starting_capital: float
    max_position_pct: float    # max single-position max-loss as fraction of starting
    max_book_pct: float        # max total max-loss across all positions as fraction of starting
    daily_loss_halt: float     # absolute $ loss for the day to trigger halt
    max_strategy_pct: float = 0.30  # max % of starting allocated to one strategy


def load_limits_from_settings(
    settings_path: str | Path = "config/settings.toml",
    starting_capital_override: float | None = None,
) -> CapitalLimits:
    """Load capital limits from settings.toml [capital] section.

    `starting_capital_override` lets the caller use the value from the cash
    table instead of the settings file (so the running balance can drift but
    the percentage limits stay relative to whatever the operator configured
    as 'starting').
    """
    path = Path(settings_path)
    if not path.exists():
        return CapitalLimits(
            starting_capital=starting_capital_override or 20000.0,
            max_position_pct=0.10,
            max_book_pct=0.50,
            daily_loss_halt=1000.0,
            max_strategy_pct=0.30,
        )
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cap = cfg.get("capital", {})
    return CapitalLimits(
        starting_capital=float(starting_capital_override or cap.get("starting", 20000.0)),
        max_position_pct=float(cap.get("max_position_pct", 0.10)),
        max_book_pct=float(cap.get("max_book_pct", 0.50)),
        daily_loss_halt=float(cap.get("daily_loss_halt", 1000.0)),
        max_strategy_pct=float(cap.get("single_strategy_pct", 0.30)),
    )


def check_proposed_order(
    db_path: str,
    proposed_max_loss: float,
    strategy_id: str | None = None,
    limits: CapitalLimits | None = None,
) -> SizeCheckResult:
    """Validate a proposed order against capital boundaries.

    `proposed_max_loss` must be a POSITIVE number representing the worst-case
    dollar loss for this single order.
    """
    conn = get_conn(db_path)

    cash_row = conn.execute(
        "SELECT starting_capital, daily_pnl, daily_loss_halt, mode FROM cash ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if cash_row is None:
        return SizeCheckResult(
            allowed=False,
            reason="No cash row in DB",
            proposed_max_loss=proposed_max_loss,
            new_total_book_risk=0,
            capital_ceiling=0,
        )

    starting, daily_pnl, daily_halt, mode = cash_row

    if mode == "halt":
        return SizeCheckResult(False, "mode=halt", proposed_max_loss, 0, float(starting))

    if daily_pnl is not None and daily_pnl <= -float(daily_halt):
        return SizeCheckResult(
            False,
            f"Daily loss halt triggered: pnl={daily_pnl} halt=-{daily_halt}",
            proposed_max_loss,
            0,
            float(starting),
        )

    # Risk = abs(open_debit) * qty * mult for ALL positions (debit AND credit).
    # Credit trades have negative open_debit; using ABS ensures they contribute
    # to book risk instead of being invisible.
    open_risk_row = conn.execute(
        """
        SELECT COALESCE(SUM(ABS(open_debit) * qty * 1000), 0)
        FROM positions WHERE status IN ('open', 'closing')
        """
    ).fetchone()
    open_risk = float(open_risk_row[0]) if open_risk_row else 0.0

    pending_risk_row = conn.execute(
        """
        SELECT COALESCE(SUM(ABS(limit_price) * qty * 1000), 0)
        FROM orders WHERE status IN ('draft','submitted')
        """
    ).fetchone()
    pending_risk = float(pending_risk_row[0]) if pending_risk_row else 0.0

    new_total = open_risk + pending_risk + proposed_max_loss
    starting_f = float(starting)

    if limits is None:
        # Fall back to loading from settings.toml. Pass the cash row's
        # starting_capital so the percentages apply to the configured pool,
        # not the rolling balance.
        try:
            limits = load_limits_from_settings(starting_capital_override=starting_f)
        except Exception:
            limits = CapitalLimits(
                starting_capital=starting_f,
                max_position_pct=0.10,
                max_book_pct=0.50,
                daily_loss_halt=float(daily_halt),
            )

    if proposed_max_loss > starting_f * limits.max_position_pct:
        return SizeCheckResult(
            False,
            f"Single position exceeds {limits.max_position_pct:.0%} of starting capital "
            f"(${proposed_max_loss:.0f} > ${starting_f * limits.max_position_pct:.0f})",
            proposed_max_loss,
            new_total,
            starting_f,
        )

    if new_total > starting_f * limits.max_book_pct:
        return SizeCheckResult(
            False,
            f"Total book risk would exceed {limits.max_book_pct:.0%} of starting capital "
            f"(${new_total:.0f} > ${starting_f * limits.max_book_pct:.0f})",
            proposed_max_loss,
            new_total,
            starting_f,
        )

    if strategy_id is not None:
        strat_risk_row = conn.execute(
            """
            SELECT COALESCE(SUM(ABS(open_debit) * qty * 1000), 0) FROM positions
            WHERE status IN ('open', 'closing') AND strategy_id = ?
            """,
            [strategy_id],
        ).fetchone()
        strat_risk = float(strat_risk_row[0]) if strat_risk_row else 0.0
        if strat_risk + proposed_max_loss > starting_f * limits.max_strategy_pct:
            return SizeCheckResult(
                False,
                f"Strategy {strategy_id} would exceed {limits.max_strategy_pct:.0%} of starting capital",
                proposed_max_loss,
                new_total,
                starting_f,
            )

    return SizeCheckResult(True, "ok", proposed_max_loss, new_total, starting_f)
