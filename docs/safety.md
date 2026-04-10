# Safety

The hard rules and the reasoning behind them.

## Capital boundary

Sizing is **always** derived from `starting_capital`, never from current equity. If you start with $20k and grow to $30k, the system continues sizing as if you had $20k. Profits accumulate but require manual withdrawal.

| Limit | Default | Why |
|---|---|---|
| starting_capital | $20,000 | The pool you're willing to lose |
| max_position_pct | 10% = $2,000 | No single trade can wipe more than 10% |
| max_book_pct | 50% = $10,000 | Max simultaneous risk across all positions |
| daily_loss_halt | -$1,000 | Stop trading for the day at 5% drawdown |
| single_strategy_pct | 30% | One strategy can't dominate the book |

These are checked **before every order**, regardless of mode. Configurable in `config/settings.toml`.

## Exit rules -- deterministic, no LLM

All exit decisions are rules-based. LLMs are never in the exit path. The position manager evaluates 6 rule families in strict priority order every tick:

1. **Time stop** -- mandatory close at DTE threshold (prevents expiry risk)
2. **Short-strike defense** -- close if underlying breaches buffer around sold strike
3. **Vol-crush exit** -- early profit-take when IV drops significantly from entry
4. **Trailing stop** -- lock in gains after profit exceeds activation threshold
5. **Base target/stop** -- percentage-based profit target and stop loss
6. **Wide-spread deferral** -- delays profit-taking when market is illiquid

Parameters per strategy in `config/strategies.yaml`. Rules gracefully degrade: missing entry context skips enhanced rules and falls back to base.

## Hard NO list

The system will NEVER:

1. Auto-promote a strategy between tiers without human approval
2. Place an order that exceeds the capital boundary
3. Place a market order on a combo (always limit)
4. Send orders without a successful reconciler check on startup
5. Trust LLM output that fails JSON parsing (rejects, logs, continues)
6. Trade after the daily loss halt has been hit
7. Trade in modes other than what's explicitly set
8. Override `mode=halt` for any reason
9. Place a trade based on news older than 30 minutes for high-impact items
10. Place a trade if any input data timestamp is > 5 min old during RTH
11. Use LLM output for exit/entry decisions (rules only)

## Human-in-the-loop checkpoints

| Decision | Required approval | Implemented |
|---|---|---|
| New strategy proposed | Human | Yes (config/strategies.yaml) |
| Strategy promoted between tiers | Human (every promotion) | Yes (tier gates enforced at load) |
| Mode change paper -> draft | Human | Yes (CLI) |
| Mode change draft -> live | Human | Yes (CLI) |
| Capital limit changes | Human (config edit + restart) | Yes |
| Order in `mode=draft` | Human (per order) | Partial (orders stage, but dashboard approve/reject UI not built) |
| Order in `mode=live` and >5% of capital | Human (double-check) | Not yet (live mode not implemented) |
| Strategy parameter change > 25% | Human | Not yet (no automated detection) |
| Adding a new news source | Human | Yes (config edit) |

## Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| Stale snapshot | pre_trade_check rejects |
| IB disconnect | reconciler refuses to act on stale state |
| Phantom position | reconciler halts trading |
| Unknown contract | order rejected at conId resolution |
| LLM hallucination | JSON parser + critic + structured outputs |
| LLM budget overrun | agents auto-halt at monthly cap |
| Daily loss halt | mode -> halt, requires manual unhalt |
| Power loss / crash | SQLite WAL is crash-safe; reconcile on restart |
| Wide bid-ask spread | deferral rule delays exits in illiquid markets |

## Emergency stop

```bash
python -m cli.tradectl halt
```

This is the most important command. Use it any time you're unsure. There is no penalty for halting and re-investigating.

## What could still go wrong

1. **Vendor risk on IB**: if IB has a bug in combo order routing, we could send wrong things. Mitigation: paper trade extensively first.
2. **LLM prompt injection**: a malicious news source could include text that manipulates the agent. Mitigation: structured outputs only, no free-form action.
3. **Strategy correlation**: two "uncorrelated" strategies might fail together in tail events. Mitigation: per-strategy AND total-book limits.
4. **Time-zone bugs**: anything involving market hours, expiry dates, or news timestamps. Mitigation: UTC storage internally, display-only timezone conversion.
5. **Decimal precision**: float math on prices can drift. Mitigation: use Decimal in orders/positions, only float for greeks/IV.
