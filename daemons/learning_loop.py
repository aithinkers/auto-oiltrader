"""Learning loop — runs weekly (Friday after close).

Job:
  1. Pull all trades from past week (and past 4 weeks for baseline)
  2. Compute per-strategy metrics: hit rate, avg P&L, Sharpe, max DD, slippage
  3. Compute correlations between strategies
  4. Identify 2-3 specific decisions that were right and 2-3 that were wrong
  5. Propose 1-2 candidate changes (parameter tweaks, new strategy variants)
  6. Run candidates through the critic agent for sanity check
  7. Write a findings report to the `findings` table
  8. Push notification to user with summary + link

Auto-promotion of strategies is DISABLED by default. The user reads the
findings report and approves changes manually via the dashboard.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("Phase 2: implement after first 2 weeks of paper trading")


if __name__ == "__main__":
    main()
