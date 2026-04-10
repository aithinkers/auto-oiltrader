"""News collector daemon.

Polls free news sources defined in config/news_sources.yaml and writes to
the `news` table. High-impact items also write a commentary line that the
dashboard streams to the user.

Sources for Phase 1:
  - EIA Weekly Petroleum Status (Wed 10:30 ET via API)
  - CME daily settlement files
  - Reuters commodities RSS
  - OPEC press releases RSS
  - Yahoo Finance energy RSS

Each item is hashed for dedup. Headlines are classified for impact via
the news_classifier agent (Haiku, cheap).
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("Phase 3: implement after stream + position manager are stable")


if __name__ == "__main__":
    main()
