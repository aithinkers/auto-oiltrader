# Strategy Catalog

How to define, test, and promote strategies.

## What a strategy is

A strategy is a Python class inheriting from `strategies.base.Strategy`. It has:
- An `id` (matches `config/strategies.yaml` key)
- A `tier` (`experimental | shadow | paper | draft | live | retired`)
- `params` (dict from yaml)
- An `evaluate(market_state, current_positions)` method that returns 0-N `StrategySignal` objects

The trader daemon loads enabled strategies on startup, evaluates each one each cycle, and routes signals through the recommendation pipeline.

## Strategy lifecycle

```
Author writes strategy --> register in strategies.yaml as experimental
   |
   v
Runs in shadow mode, P&L tracked but no orders
   |
   v
Weekly evaluator reviews after 4+ weeks
   |
   v
Operator approves promotion to paper
   |
   v
Paper trades for 4+ weeks
   |
   v
Operator approves promotion to draft
   |
   v
Real orders staged for human approval
   |
   v
Operator approves promotion to live
   |
   v
Auto-executed (with capital limits enforced)
```

Demotion can happen at any time, manually or automatically (Sharpe < 0 over 10 trades).

## Built-in strategies

### Credit strategies (paper tier)

**`iron_condor_range_lo`** -- Range-bound premium collection
- Sells delta-targeted iron condors on LO
- Entry: ATM IV > threshold, DTE in target range
- Exit: 50% profit target, 100% stop loss, DTE <= 3 time stop
- Enhanced: vol-crush exit, trailing stop, short-strike defense, wide-spread deferral

**`bull_put_credit_lo`** -- Bullish credit spread
- Sells put spreads when bullish signal detected
- Same enhanced exit rules as iron condor

**`bear_call_credit_lo`** -- Bearish credit spread
- Sells call spreads when bearish signal detected
- Same enhanced exit rules as iron condor

### Debit strategies (experimental tier)

**`bull_call_debit_lo`** -- Bullish debit spread
- Buys call spreads for directional upside
- Enhanced: trailing stop, wide-spread deferral (no vol-crush or short-strike)

**`bear_put_debit_lo`** -- Bearish debit spread
- Buys put spreads for directional downside
- Same enhanced rules as bull call debit

### Other strategies (experimental tier)

**`butterfly_pin_lo`** -- Long butterfly targeting strike pin at expiry

**`vol_crush_post_event`** -- Sells short premium after binary events resolve and IV collapses

**`long_strangle_event`** -- Buys strangles before anticipated high-vol events

**`eia_wednesday`** -- Buys straddle/strangle Tuesday, exits after Wednesday EIA report

## Exit rule parameters

Per-strategy in `config/strategies.yaml`:

```yaml
strategy_name:
  # Base exit rules (required)
  profit_target_pct: 0.50      # close at 50% of max profit
  stop_loss_pct: 1.00          # close at 100% loss of premium
  time_stop_dte: 3             # close at 3 DTE

  # Enhanced exit rules (optional, all nullable)
  vol_crush_exit_pts: 10       # close if IV dropped 10+ pts from entry (credit only)
  trail_activate_pct: 0.60     # activate trailing stop at 60% of max profit
  trail_giveback_pct: 0.30     # close if profit retraces 30% from peak
  short_strike_buffer_pct: 0.015  # close if underlying within 1.5% of short strike
  min_combo_spread_pct: 0.35   # defer target exit if bid-ask > 35% of combo value
```

Rules evaluate in priority order. Missing parameters disable that rule gracefully.

## Adding a new strategy

1. Create `strategies/my_strategy.py` inheriting from `Strategy`
2. Implement `evaluate(market_state, current_positions)` returning `StrategySignal` objects
3. Add entry to `config/strategies.yaml`:
   ```yaml
   my_strategy:
     name: "My new strategy"
     tier: experimental
     enabled: true
     max_concurrent: 2
     target_dte_min: 7
     target_dte_max: 45
     profit_target_pct: 0.50
     stop_loss_pct: 1.00
     time_stop_dte: 3
   ```
4. Add tests in `tests/`
5. Restart the daemon -- it auto-loads from yaml

## Tier promotion criteria

| From -> To | Min duration | Sharpe | Max DD | Other |
|---|---|---|---|---|
| experimental -> shadow | 5 trading days | > 0 | < 10% | > 5 trades |
| shadow -> paper | 4 weeks | > 1.0 | < 5% | correlation < 0.6 with existing |
| paper -> draft | 4 weeks | > 1.0 | < 5% | profit > LLM costs |
| draft -> live | 4 weeks | n/a | n/a | 70%+ user approval rate |

These are minimums. The critic agent and human operator both have veto power.
