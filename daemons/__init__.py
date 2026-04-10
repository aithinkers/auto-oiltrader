"""Long-running processes.

Each daemon is independently restartable. They communicate via the DuckDB store
plus the parquet snapshot directory. Only `trader_daemon` writes to the
orders/positions tables — single-writer rule.
"""
