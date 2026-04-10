"""News classifier — Haiku, classifies headlines for impact / sentiment.

Called by news_collector for each new headline. Updates `news.sentiment`,
`news.impact`, and `news.symbols_affected`.

This is the cheapest agent (Haiku, short prompts). Designed to handle
50-100 items/day for under $1.
"""

from __future__ import annotations


def classify(headline: str, body: str, db_path: str) -> dict:
    raise NotImplementedError("Phase 3")
