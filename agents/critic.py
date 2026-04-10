"""Critic agent — reviews proposed strategy changes before promotion.

When the learning_loop proposes a strategy promotion or parameter change,
the critic reviews it for:
  - Obvious overfit (perfect backtest, untested in different regimes)
  - Correlation with existing live strategies (don't compound the same risk)
  - Missing tail-risk consideration
  - Sample size sufficiency

Output: 'approve' | 'reject' | 'request_more_evidence' with a 1-paragraph reason.
The critic's recommendation is logged but the human still has final say.

Uses Claude Sonnet (judgment matters here).
"""

from __future__ import annotations


def review(proposal: dict, db_path: str) -> dict:
    raise NotImplementedError("Phase 3")
