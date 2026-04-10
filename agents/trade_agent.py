"""Trade agent — converts approved recommendations into orders.

Triggered when a new recommendation arrives or a draft is approved.
Job:
  1. Resolve conIds for each leg
  2. Build the IB BAG combo
  3. Run pre-trade checks (sizing, gap-risk, news veto)
  4. Pull live combo bid/ask from IB
  5. Decide initial limit price (mid by default, or use walking strategy)
  6. Insert an order row in 'draft' or 'submitted' state depending on mode
  7. Hand off to trader_daemon for execution

Mostly deterministic. LLM is only invoked for edge-case judgment calls.
"""

from __future__ import annotations


def run() -> None:
    raise NotImplementedError("Phase 2: build after analysis_agent")


if __name__ == "__main__":
    run()
