# Contributing

Thanks for your interest in contributing to Autonomous Oil Trader.

## Getting started

1. Fork the repo and clone locally
2. Install dependencies: `uv sync` or `pip install -e ".[dev]"`
3. Copy `.env.example` to `.env` and fill in credentials
4. Run tests: `python -m pytest -q`

## Development workflow

1. Create a feature branch from `main`
2. Make your changes
3. Run the test suite: `python -m pytest -q`
4. Run the linter: `ruff check .`
5. Open a pull request with a clear description

## Code style

- Python 3.12+, type hints encouraged
- Ruff for linting and formatting (config in `pyproject.toml`)
- Line length: 110 characters
- Tests go in `tests/` with `test_` prefix

## Architecture guidelines

- **Exit decisions are rules-based only.** LLMs are used for narration, analysis, and advisory -- never for real-time exit/entry decisions.
- **SQLite WAL mode** is the primary datastore. All timestamps stored in UTC ISO 8601 with `Z` suffix.
- **Single-writer rule**: only the trader daemon writes to orders/positions tables.
- **Safety first**: all capital limits are checked before every order. When in doubt, halt.
- **Strategies are YAML-driven**: parameters live in `config/strategies.yaml`, not hardcoded.

## Adding a strategy

1. Create `strategies/my_strategy.py` inheriting from `strategies.base.Strategy`
2. Implement `evaluate(market_state, current_positions)` returning `StrategySignal` objects
3. Add entry to `config/strategies.yaml` with `tier: experimental`
4. Add tests in `tests/`
5. The strategy will run in shadow mode until promoted

## Reporting issues

- Include your Python version, OS, and IB Gateway/TWS version
- Paste the relevant log output from `logs/`
- For trading bugs, include the position ID and relevant DB state

## What not to change without discussion

- Capital safety limits in `core/risk.py` or `core/sizing.py`
- The single-writer architecture
- Exit rule priority order
- Tier promotion gates
