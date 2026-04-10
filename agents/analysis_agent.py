"""Analysis agent — runs every 15 min during RTH.

Reads:
  - Latest snapshot, futures bars, recent news, open positions, user observations, patterns
Calls Claude with skills/analyze_smile.md as the main skill prompt.
Writes:
  - 0-3 recommendations to the DB
  - A commentary line summarizing the regime
  - A decision row for audit

STUB — implements in Phase 2.
"""

from __future__ import annotations


def run() -> None:
    raise NotImplementedError("Phase 2: implement after rules-based strategies are validated")


if __name__ == "__main__":
    run()
