"""Shared dashboard DB helpers — sqlite read-only with pandas convenience."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Make sure project root is on sys.path so we can import core.timefmt
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.timefmt import fmt_local  # noqa: E402


DB_PATH = os.environ.get("TRADER_DB_PATH", "./data/trader.db")


def _db_mtime() -> float:
    """Return mtime of the DB file. Used as a cache key so a file rotation
    (e.g. tradectl init-db --force creating a new inode) forces a fresh
    connection without restarting Streamlit."""
    try:
        return os.stat(DB_PATH).st_mtime
    except FileNotFoundError:
        return 0.0


@st.cache_resource
def _open_conn(db_path: str, mtime: float) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_conn() -> sqlite3.Connection:
    """Open a read-only SQLite connection. WAL mode allows this even when a
    daemon process holds the writer lock."""
    return _open_conn(DB_PATH, _db_mtime())


def db_exists() -> bool:
    return Path(DB_PATH).exists()


def query_df(
    sql: str,
    params: list | tuple = (),
    *,
    localize_columns: list[str] | tuple[str, ...] = (
        "ts", "ts_opened", "ts_closed", "ts_created", "ts_submitted",
        "ts_filled", "applied_at", "approved_at", "expires_at",
        "period_start", "period_end",
    ),
) -> pd.DataFrame:
    """Run a query and return a pandas DataFrame.

    Any column in `localize_columns` that exists in the result is converted
    from stored UTC ISO format to the configured display timezone (settings.toml
    [display] timezone). Pass an empty tuple to skip conversion.
    """
    conn = get_conn()
    df = pd.read_sql_query(sql, conn, params=params)
    if localize_columns:
        for col in localize_columns:
            if col in df.columns:
                df[col] = df[col].map(lambda v: fmt_local(v) if v is not None else "")
    return df


def query_one(sql: str, params: list | tuple = ()) -> dict | None:
    conn = get_conn()
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
