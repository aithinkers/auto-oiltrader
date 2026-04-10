"""Query helpers for the latest market state.

Reads parquet snapshots written by the stream daemon.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd


def latest_snapshot(snapshot_dir: str | Path, date_str: str | None = None) -> pd.DataFrame:
    """Return the rows of the most recent snapshot file (across all classes/expiries)."""
    base = Path(snapshot_dir)
    if date_str:
        glob = str(base / f"date={date_str}" / "*.parquet")
    else:
        # Find the most recent date partition
        partitions = sorted(p.name for p in base.glob("date=*"))
        if not partitions:
            return pd.DataFrame()
        glob = str(base / partitions[-1] / "*.parquet")

    con = duckdb.connect()
    sql = f"""
        WITH src AS (SELECT * FROM read_parquet('{glob}'))
        SELECT * FROM src WHERE ts = (SELECT max(ts) FROM src)
    """
    try:
        return con.execute(sql).fetchdf()
    except duckdb.IOException:
        return pd.DataFrame()


def get_underlying_last(snapshot_dir: str | Path, local_symbol: str) -> float:
    """Return the most recent last/mid for a futures local_symbol (e.g. 'CLK6')."""
    snap = latest_snapshot(snapshot_dir)
    if snap.empty:
        return float("nan")
    rows = snap[(snap["sec_type"] == "FUT") & (snap["local_symbol"] == local_symbol)]
    if rows.empty:
        return float("nan")
    r = rows.iloc[0]
    if r["last"] == r["last"] and r["last"] > 0:
        return float(r["last"])
    if r["bid"] == r["bid"] and r["ask"] == r["ask"]:
        return (float(r["bid"]) + float(r["ask"])) / 2
    return float("nan")


def get_atm_iv(snapshot_dir: str | Path, trading_class: str, expiry: str) -> float:
    """Return the mean ATM IV (closest call+put to underlying) for a given expiry."""
    snap = latest_snapshot(snapshot_dir)
    if snap.empty:
        return float("nan")
    sub = snap[
        (snap["sec_type"] == "FOP")
        & (snap["trading_class"] == trading_class)
        & (snap["expiry"] == expiry)
        & (snap["iv"] > 0)
    ].copy()
    if sub.empty:
        return float("nan")
    spot = sub["underlying_last"].iloc[0]
    if not (spot == spot):
        return float("nan")
    sub["dist"] = (sub["strike"] - spot).abs()
    closest = sub.sort_values("dist").head(2)
    return float(closest["iv"].mean())
