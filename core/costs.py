"""Cost tracking — commissions, LLM tokens, data fees.

Net P&L on the dashboard always subtracts these. Costs are immutable; once
written they stay.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from core.db import transaction


# Anthropic pricing as of 2025 (USD per million tokens). Update as rates change.
ANTHROPIC_PRICING = {
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6":          {"input":  3.00, "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input":  0.80, "output":  4.00},
}


def llm_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Compute USD cost of a Claude API call."""
    pricing = ANTHROPIC_PRICING.get(model)
    if pricing is None:
        return 0.0
    return (tokens_in / 1_000_000) * pricing["input"] + (tokens_out / 1_000_000) * pricing["output"]


def record_llm_cost(
    db_path: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    context: str | None = None,
) -> float:
    """Insert a cost row for an LLM call. Returns the dollar amount."""
    amount = llm_cost(model, tokens_in, tokens_out)
    if amount <= 0:
        return 0.0
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO costs (ts, category, detail, amount, context) VALUES (?, ?, ?, ?, ?)",
            [datetime.now(), "llm", f"{model} {tokens_in}/{tokens_out}", amount, context or ""],
        )
    return amount


def record_commission(
    db_path: str,
    amount: float,
    detail: str,
    context: str | None = None,
) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO costs (ts, category, detail, amount, context) VALUES (?, ?, ?, ?, ?)",
            [datetime.now(), "commission", detail, amount, context or ""],
        )


def monthly_llm_spend(db_path: str) -> float:
    """Sum LLM costs for the current calendar month."""
    from core.db import get_conn
    conn = get_conn(db_path)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) FROM costs
        WHERE category='llm' AND ts >= date_trunc('month', now())
        """
    ).fetchone()
    return float(row[0]) if row else 0.0


def is_llm_budget_exceeded(db_path: str, monthly_budget: float) -> bool:
    return monthly_llm_spend(db_path) >= monthly_budget
