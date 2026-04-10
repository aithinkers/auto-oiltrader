"""Evaluator — weekly performance review and findings report.

Runs Friday after close. Writes a markdown report to the `findings` table
with:
  - Per-strategy P&L, hit rate, Sharpe, max DD
  - Slippage analysis
  - 2-3 specific decisions that worked, 2-3 that didn't, with explanations
  - Proposed changes (parameter tweaks, candidate promotions/demotions)
  - Cost summary (commissions + LLM)

The report is pushed to the user via ntfy with a link to the dashboard
findings page.
"""

from __future__ import annotations


def run_weekly_review(db_path: str) -> str:
    """Returns the path to the generated markdown report."""
    raise NotImplementedError("Phase 3")
