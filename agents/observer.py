"""Observer — processes user observations into structured signals.

When the user submits a free-text observation through the dashboard or API,
this agent classifies it (impact, sentiment, symbols affected, expiry hint)
and writes a structured news row.

Uses Claude Haiku.
"""

from __future__ import annotations


def process_observation(text: str, db_path: str) -> dict:
    raise NotImplementedError("Phase 3")
