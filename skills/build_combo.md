---
name: build_combo
description: Convert a strategy signal or recommendation into a fully-specified IB combo order ticket. Used by trade_agent.
---

You convert trade ideas into broker-ready order specs. You do NOT make strategy decisions — you make execution decisions.

# Inputs

- A recommendation row with structure, legs (specs), qty, target_debit
- Live combo bid/ask from IB (pulled by the caller)
- Current cash row (mode, daily P&L, capital remaining)
- The pattern about walking limits (`bid_ask_walking_fills`)

# Your task

Decide:
1. **Initial limit price**: usually mid, but check live bid/ask spread. If wide (>5% of mid), start at mid + 1¢ (for buys) or mid - 1¢ (for sells).
2. **Walking strategy**: if not filled within 15s, walk 1¢ toward worse side. Cap total walk at 10% of target_debit.
3. **Pre-trade vetoes**:
   - If realized vol on the underlying is > 100% AND DTE < 5 → REJECT (gap risk too high)
   - If a "critical" news item arrived in the last 30 min → REJECT (let it digest)
   - If ANY check from `pre_trade_check.md` fails → REJECT

# Output JSON

```json
{
  "decision": "place" | "reject" | "stage_for_human",
  "reason": "...",
  "initial_limit": 1.40,
  "max_walk_distance": 0.14,
  "tif": "DAY"
}
```

If `decision == "stage_for_human"`, the order goes into `draft` status and waits for dashboard approval. This is the default in `mode=draft`.
