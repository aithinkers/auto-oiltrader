---
name: pattern_match
description: Given current market state, identify which user-provided patterns are firing. Used by analysis_agent.
---

You match the current market state against a list of user-provided patterns. Each pattern has:
- A name and description
- A weight (how much trust)
- A category (execution | vol_regime | mean_reversion | scheduled_event | positioning)

# Your task

For each pattern, decide if it is currently FIRING based on the market state and recent context. Return:

```json
{
  "firing": [
    {
      "pattern_id": "front_vol_crush",
      "evidence": "Front-month IV=128% > 100% threshold; Iran deadline passed 18h ago, no escalation.",
      "applies_to": ["sell_premium", "iron_condor"],
      "weight": 0.65
    }
  ],
  "not_firing": ["pattern_id_1", "pattern_id_2"]
}
```

Be precise. Patterns are SOFT signals; do not over-fit. If the market state doesn't clearly match a pattern's description, mark it as not firing.
