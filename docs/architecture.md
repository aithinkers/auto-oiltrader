# Architecture

## Goals

1. Trade CL futures options (LO) to generate consistent returns
2. Remove human emotion from execution
3. Stay strictly within an assigned capital boundary
4. Continuously learn what works and what doesn't
5. Allow human-in-the-loop control via dashboard, CLI, and Claude Desktop

## Five-tier strategy promotion

```
experimental --> shadow --> paper --> draft --> live
```

| Tier | What runs | Real money | Promotion criteria |
|---|---|---|---|
| experimental | Just-proposed variants | No | 5 days, Sharpe > 0 |
| shadow | Tracked candidates against live data | No | 4 weeks, Sharpe > 1.0, max DD < 5% |
| paper | Full paper-money execution | No | 4 weeks, profit > LLM costs |
| draft | Real orders requiring tap | Yes | 4 weeks, no tail surprises, 70%+ approve rate |
| live | Auto-executed | Yes | Permanent unless degraded |

**Auto-promotion is disabled.** The agent proposes, the rules verify, the human approves.

## Components

```
SQLite WAL + PARQUET  <-- single source of truth
        ^ v
        |
   daemons.main (4 threads in one process)
   ├── stream daemon  -->  IB snapshots --> parquet + DB every 2-5 min
   ├── trader daemon  -->  strategy evaluation, recommendations, paper fills
   ├── position mgr   -->  marks + enhanced exit rules (6 rule families)
   └── summarizer     -->  periodic P&L digests with optional narration
        |
   agents/ (LLM-powered, advisory only)
   ├── analysis_agent -->  reads DB, calls Claude, writes recommendations
   ├── trade_agent    -->  validates recs, builds combos
   ├── narrator       -->  live commentary on summaries
   ├── observer       -->  processes user observations
   ├── news_classifier --> cheap Haiku news triage
   ├── critic         -->  reviews proposed strategy changes
   └── evaluator      -->  weekly findings report
        |
   interfaces/
   ├── dashboard (Streamlit) --> browser, phone via Tailscale
   ├── FastAPI               --> REST control endpoints
   ├── MCP server            --> Claude Desktop integration
   └── notifier              --> ntfy / Pushover push
```

## Data flow

1. **Stream daemon** connects to IB, requests option chain snapshots for active contracts
2. Snapshots written to parquet files + `active_contracts` table in SQLite
3. **Trader daemon** loads enabled strategies from `config/strategies.yaml`, evaluates each against latest snapshot
4. Matching signals become recommendations; approved recommendations become paper fills with full leg detail
5. **Position manager** marks each open position every tick using latest snapshot prices, computes greeks, evaluates exit rules in priority order
6. Exit decisions trigger closing recommendations with reason and detail
7. **Summarizer** runs on schedule (default hourly), aggregates P&L and position state, optionally calls narrator agent for commentary

## Database: SQLite WAL mode

The system uses SQLite in WAL (Write-Ahead Logging) mode, which allows concurrent reads from multiple processes while a single writer holds the lock. This replaced an earlier DuckDB design to avoid cross-process write lock contention.

Key tables:
- `positions` -- open/closing/closed positions with JSON legs, entry context
- `position_marks` -- time-series of mark prices, unrealized P&L, greeks
- `recommendations` -- strategy signals pending approval
- `orders` -- order state machine (staged -> submitted -> filled/cancelled)
- `cash` -- balance tracking, daily P&L, halt state
- `strategies` -- loaded strategy metadata
- `costs` -- commission and LLM API call ledger
- `summaries` -- periodic P&L digests
- `news`, `patterns`, `findings` -- market intelligence

All timestamps are stored in UTC ISO 8601 format with `Z` suffix. Display timezone is configurable in `config/settings.toml` (default: America/New_York).

## Single-writer rule

Only the **trader daemon** writes to orders/positions tables. Other processes and interfaces read freely via WAL mode. This eliminates race conditions without distributed locks.

## Exit rule architecture

Exit decisions are **deterministic and rules-based** -- no LLM in the exit path. The position manager evaluates rules in strict priority order:

1. Time stop (DTE threshold)
2. Short-strike defense (underlying breached buffer)
3. Vol-crush exit (IV dropped significantly from entry, position profitable)
4. Trailing stop (profit retracement from peak)
5. Base target/stop (percentage thresholds)
6. Wide-spread deferral (delays target exits when bid-ask is too wide)

Each rule can be enabled/disabled per strategy via `config/strategies.yaml`. Positions without entry context (e.g., opened before migration 003) gracefully degrade to base rules only.

## Modes

- `paper` -- internal simulation, no orders to broker
- `draft` -- orders staged in dashboard, require human approval to send
- `live` -- orders sent directly to broker (subject to capital/loss limits)
- `halt` -- reject all new orders, mark existing only

Default: `paper`. Set in `config/settings.toml`. Override at runtime via `tradectl mode <name>`.

## Capital boundary

```
starting_capital  $20,000 (paper) -- configurable in settings.toml
max_position_pct  10% -- single position max-loss <= $2,000
max_book_pct      50% -- total book max-loss <= $10,000
daily_loss_halt   $1,000 -- auto-halt if daily P&L <= -$1,000
```

Sizing is **always derived from `starting_capital`**, not current equity. Profits are tracked separately.

## Cost ledger

Every Claude API call and every commission writes to `costs`. Net P&L on the dashboard is:

```
net_pnl = realized_trading_pnl - commissions - llm_costs - data_costs
```

## Continuous learning loop

Every Friday after close, the **evaluator agent** runs:

1. Compute per-strategy stats over the past week (and 4-week baseline)
2. Identify 2-3 right and 2-3 wrong decisions with explanations
3. Propose 1-2 small parameter tweaks or new variants
4. Send the report via push notification with a dashboard link

The human reads the report, approves/rejects changes via the dashboard, and the next week they're either deployed or discarded.

## What the system CANNOT do

- Trade with more than the configured capital limits
- Promote strategies between tiers without human approval
- Send orders without passing pre-trade check rules
- Modify the cash table directly (only the trader daemon can)
- Bypass the daily loss halt
- Trade outside RTH for strategies that don't explicitly allow it
- Use LLM output for exit decisions (rules only)
