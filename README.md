# Autonomous Oil Trader

An LLM-assisted, rules-driven trading system for crude oil futures options (NYMEX CL / LO) built on Interactive Brokers, SQLite, and Claude. Paper-first design with five-tier strategy promotion, deterministic exit rules, and human-in-the-loop safety gates.

> **Status**: Paper trading is fully operational. Draft/live modes, the REST API, and dashboard approval workflows are scaffolded but not yet complete. See [Current Limitations](#current-limitations) below.

## What it does

- Streams live option chain snapshots from IB every 2-5 minutes
- Evaluates multiple option strategies (credit spreads, debit spreads, iron condors) against configurable entry rules
- Opens paper (simulated) positions with full leg detail and greek tracking
- Marks positions every tick and evaluates rule-based exits: profit targets, stop losses, time stops, vol-crush exits, trailing stops, short-strike defense, and wide-spread deferral
- LLM agents narrate trade rationale, classify news, generate weekly performance reviews, and propose strategy variants -- but never make exit decisions
- Streamlit dashboard for monitoring positions, P&L, recommendations, and summaries
- CLI (`tradectl`) for status, halt, mode changes, and DB management

## Quick start

```bash
git clone https://github.com/aithinkers/auto-oiltrader.git
cd autonomous-oiltrader

# Install dependencies
uv sync                        # or: pip install -e ".[dev]"

# Configure
cp .env.example .env           # fill in IB + Anthropic credentials
vi config/settings.toml        # review capital limits, IB connection

# Initialize database
python -m cli.tradectl init-db
python -m cli.tradectl migrate
python -m cli.tradectl status  # verify: mode=paper, $20k starting capital

# Start the system
python -m daemons.main --log-level INFO &
streamlit run interfaces/dashboard/app.py --server.port 8511 &
```

See [docs/install.md](docs/install.md) for full setup including IB Gateway/TWS configuration.

## Prerequisites

- Python 3.12+
- Interactive Brokers account with TWS or IB Gateway
- Anthropic API key (for LLM agents -- narration, news, weekly review)
- macOS or Linux

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  daemons.main                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │  stream   │ │  trader   │ │ position │ │summary │ │
│  │  daemon   │ │  daemon   │ │ manager  │ │  izer  │ │
│  └─────┬────┘ └─────┬────┘ └─────┬────┘ └───┬────┘ │
└────────┼────────────┼────────────┼───────────┼──────┘
         │            │            │           │
         ▼            ▼            ▼           ▼
    ┌─────────────────────────────────────────────┐
    │          SQLite WAL (data/trader.db)         │
    └─────────────────────────────────────────────┘
         ▲            ▲            ▲
         │            │            │
    ┌────┴───┐  ┌─────┴────┐  ┌───┴──────┐
    │  CLI   │  │Dashboard │  │  Agents  │
    │tradectl│  │Streamlit │  │ (Claude) │
    └────────┘  └──────────┘  └──────────┘
```

A single `daemons.main` process runs 4 worker threads sharing one SQLite WAL-mode database:

- **Stream daemon** -- connects to IB, writes option chain snapshots (parquet) and DB updates
- **Trader daemon** -- evaluates strategies, generates recommendations, opens paper positions
- **Position manager** -- marks open positions, computes greeks, runs exit rules every tick
- **Summarizer** -- generates periodic P&L digests with optional LLM narration

LLM agents (narrator, news classifier, trade analyst, weekly evaluator) read the DB, call Claude, and write recommendations/commentary back. They advise -- they don't decide.

## Strategies

Defined in `config/strategies.yaml`. Each strategy has entry rules, exit parameters, and a promotion tier.

| Strategy | Structure | Tier | Description |
|---|---|---|---|
| `iron_condor_range_lo` | Iron condor | paper | Range-bound premium collection |
| `bull_put_credit_lo` | Bull put spread | paper | Bullish credit spread |
| `bear_call_credit_lo` | Bear call spread | paper | Bearish credit spread |
| `bull_call_debit_lo` | Bull call spread | experimental | Bullish debit spread |
| `bear_put_debit_lo` | Bear put spread | experimental | Bearish debit spread |
| `butterfly_pin_lo` | Butterfly | experimental | Pin-risk play |
| `vol_crush_post_event` | Short premium | experimental | Post-event IV collapse |
| `long_strangle_event` | Long strangle | experimental | Pre-event vol play |
| `eia_wednesday` | Strangle | experimental | EIA report straddle |

See [docs/strategies.md](docs/strategies.md) for details on adding and promoting strategies.

## Exit rules

All exits are deterministic and rules-based (no LLM in the loop). Evaluated in priority order:

1. **Time stop** -- close at configurable DTE threshold
2. **Short-strike defense** -- close if underlying breaches short strike buffer (credit trades)
3. **Vol-crush exit** -- close profitable credit trades when IV drops significantly from entry
4. **Trailing stop** -- lock in profits after reaching activation threshold
5. **Base target/stop** -- percentage-based profit target and stop loss
6. **Wide-spread deferral** -- delay profit-taking when bid-ask spread is too wide (never defers stops)

Parameters are per-strategy in `config/strategies.yaml`. Existing positions without entry context gracefully fall back to base rules.

## Five-tier strategy promotion

```
experimental --> shadow --> paper --> draft --> live
```

Promotion requires human approval at every gate, backed by statistical evidence (Sharpe, drawdown, hit rate). The system proposes; the human decides.

## Safety

- **Capital boundary**: sizing derived from `starting_capital`, never current equity
- **Position limits**: max 10% risk per position, 50% total book
- **Daily loss halt**: auto-halts at configurable drawdown (default -$1,000)
- **Emergency stop**: `tradectl halt` works in any mode, immediately
- **No market orders**: combos always use limit orders
- **Cost tracking**: every commission and LLM API call logged to `costs` table

See [docs/safety.md](docs/safety.md) for the full safety model.

## Dashboard

Streamlit app with 8 pages:

| Page | What it shows |
|---|---|
| Home | Mode, balance, daily P&L, open position count |
| Overview | Equity curve, system status |
| Positions | Open/closing/closed positions with leg details, marks, greeks |
| Recommendations | Strategy signals, approve/reject UI |
| Strategies | Strategy library, tiers, performance |
| News & Observations | News feed, user observations |
| Findings | Weekly performance reports |
| Costs | Commission + LLM spend tracker |
| Summaries | Periodic P&L digests with AI commentary |

## CLI

```
tradectl status              # system mode, capital, daily P&L
tradectl positions           # list open positions
tradectl recommendations     # view pending recommendations
tradectl mode <name>         # set mode (paper|draft|live|halt)
tradectl halt                # emergency stop
tradectl unhalt              # resume in paper mode
tradectl init-db             # initialize database (non-destructive)
tradectl migrate             # apply pending schema migrations
tradectl observe <text>      # submit a market observation
tradectl costs [--month]     # show cost ledger
```

## Project layout

```
autonomous-oiltrader/
├── core/           Business logic (risk, pricing, sizing, combos, DB)
├── strategies/     Strategy implementations (base class + 9 strategies)
├── daemons/        Worker threads (stream, trader, positions, summarizer)
├── agents/         LLM-powered agents (narrator, critic, evaluator, ...)
├── skills/         Markdown prompt templates loaded by agents
├── interfaces/     Dashboard (Streamlit), API (FastAPI), MCP server
├── cli/            tradectl command-line tool
├── services/       systemd / launchd unit files
├── config/         Settings, strategies, patterns, news sources
├── db/             SQLite schema, seed data, migrations
├── tests/          pytest test suite
└── docs/           Architecture, install, runbook, safety, strategies
```

## Configuration

| File | Purpose |
|---|---|
| `.env` | Secrets (IB credentials, API keys) -- never committed |
| `config/settings.toml` | Capital limits, IB connection, mode, display timezone |
| `config/strategies.yaml` | Strategy definitions, parameters, tiers |
| `config/patterns.yaml` | Technical pattern definitions |
| `config/news_sources.yaml` | RSS/EIA/CME feed URLs |

## Docs

- [Architecture](docs/architecture.md) -- system design, components, phasing
- [Install](docs/install.md) -- new machine setup, IB configuration
- [Runbook](docs/runbook.md) -- operational procedures, failure recovery
- [Safety](docs/safety.md) -- capital limits, hard rules, failure modes
- [Strategies](docs/strategies.md) -- strategy catalog, promotion criteria

## Tests

```bash
python -m pytest -q           # run full suite
python -m pytest tests/test_risk.py -xvs   # run specific test file
```

## Current limitations

This project is under active development. The following features are documented but not yet fully implemented:

| Feature | Status |
|---|---|
| Paper mode (full cycle) | Working |
| Rules-based exit engine (6 rule families) | Working |
| Streamlit dashboard (monitoring) | Working |
| CLI (`tradectl`) | Working |
| Strategy tier enforcement | Working (experimental/shadow tiers blocked from execution) |
| Draft mode (staged orders + human approval) | Working for approval tracking; broker-submit path not yet built |
| Live mode (IB order submission) | Not implemented |
| REST API (`interfaces/api.py`) | Working (status, positions, recommendations, approve/reject, halt, mode, observations) |
| Dashboard approve/reject buttons | Working via CLI, API, and dashboard |
| News-driven DTE policy fields | Partially wired -- `realized_vol_10d` and news context not yet populated |
| Reconciler (IB <-> DB sync) | Scaffolded |
| Learning loop (weekly promotion) | Scaffolded |

Contributions welcome -- see [CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

This software is provided as-is for educational and research purposes. It is not financial advice. Trading futures and options involves substantial risk of loss. Use at your own risk. The authors are not responsible for any financial losses incurred through use of this software.

## License

MIT License. See [LICENSE](LICENSE).

## Want something like this for your own strategies?

This repo is a working reference design. If you want a tailored version --
different asset class, different broker, your own strategies, production-grade
risk controls, or a walkthrough of how this one works -- we can help.

**AI Thinkers** -- ai_trading@aithinkers.com
