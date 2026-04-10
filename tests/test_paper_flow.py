"""End-to-end paper flow integration test.

Sets up a fresh DB, writes a fake snapshot file, inserts an opening
recommendation, runs the trader_daemon tick to generate a paper fill,
runs the position_manager to mark and detect an exit, runs the trader_daemon
again to close the position, then verifies the realized P&L matches.

This test validates the entire phase-1 internal pipeline without IB.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from core.db import (
    get_conn,
    get_current_cash,
    init_schema,
    insert_recommendation,
    list_open_positions_full,
    transaction,
)
from daemons.position_manager import PositionManager
from daemons.trader_daemon import TraderDaemon


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    schema = Path(__file__).parent.parent / "db" / "schema.sql"
    seed = Path(__file__).parent.parent / "db" / "seed.sql"
    init_schema(db, schema)
    import sqlite3
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.executescript(seed.read_text())
    finally:
        conn.close()
    yield db
    from core.db import close_cached
    close_cached(str(db))


@pytest.fixture
def snapshot_dir(tmp_path: Path) -> Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


@pytest.fixture
def settings(snapshot_dir: Path) -> dict:
    return {
        "stream": {"snapshot_interval_sec": 1},
        "paths": {
            "snapshot_dir": str(snapshot_dir),
            "db_path": "",
        },
    }


def write_snapshot(snapshot_dir: Path, rows: list[dict], date_str: str | None = None) -> None:
    """Write a snapshot parquet file with the given rows."""
    ts = datetime.now()
    if date_str is None:
        date_str = ts.strftime("%Y-%m-%d")
    out_dir = snapshot_dir / f"date={date_str}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"snap_{ts.strftime('%H%M%S%f')}.parquet"
    table = pa.Table.from_pylist([{**r, "ts": ts} for r in rows])
    pq.write_table(table, out_path, compression="zstd")


def make_fop_row(strike: float, right: str, mid: float, expiry: str = "20260516") -> dict:
    """Build one snapshot row for a given FOP leg."""
    return {
        "sec_type": "FOP",
        "trading_class": "LO",
        "expiry": expiry,
        "strike": float(strike),
        "right": right,
        "local_symbol": "",
        "underlying_local_symbol": "CLM6",
        "bid": mid - 0.01,
        "ask": mid + 0.01,
        "mid": mid,
        "last": mid,
        "volume": 0,
        "open_interest": 100,
        "iv": 0.60,
        "delta": -0.20 if right == "P" else 0.20,
        "gamma": 0.01,
        "vega": 0.10,
        "theta": -0.02,
        "underlying_last": 95.0,
    }


def make_fut_row(local_symbol: str, last: float) -> dict:
    return {
        "sec_type": "FUT",
        "trading_class": "",
        "expiry": "20260519",
        "strike": 0.0,
        "right": "",
        "local_symbol": local_symbol,
        "underlying_local_symbol": local_symbol,
        "bid": last - 0.01,
        "ask": last + 0.01,
        "mid": last,
        "last": last,
        "volume": 1000,
        "open_interest": 50000,
        "iv": 0.0,
        "delta": 0.0,
        "gamma": 0.0,
        "vega": 0.0,
        "theta": 0.0,
        "underlying_last": last,
    }


# ---------------------------------------------------------------------------
# Helpers to build a deterministic iron condor recommendation
# ---------------------------------------------------------------------------
def make_iron_condor_legs() -> list[dict]:
    """4-leg short iron condor on LO 20260516, body 90/100, wings 85/105."""
    return [
        {"trading_class": "LO", "expiry": "20260516", "strike": 90, "right": "P", "ratio": 1, "action": "SELL"},
        {"trading_class": "LO", "expiry": "20260516", "strike": 85, "right": "P", "ratio": 1, "action": "BUY"},
        {"trading_class": "LO", "expiry": "20260516", "strike": 100, "right": "C", "ratio": 1, "action": "SELL"},
        {"trading_class": "LO", "expiry": "20260516", "strike": 105, "right": "C", "ratio": 1, "action": "BUY"},
    ]


def write_chain(snapshot_dir: Path, mids: dict[tuple[float, str], float]) -> None:
    """Write a snapshot with the given strike→mid prices for the iron condor."""
    rows = [make_fut_row("CLM6", 95.0)]
    rows += [make_fop_row(k, r, mid) for (k, r), mid in mids.items()]
    write_snapshot(snapshot_dir, rows)


# ---------------------------------------------------------------------------
# The integration test
# ---------------------------------------------------------------------------
def test_paper_flow_iron_condor_target_exit(fresh_db: Path, snapshot_dir: Path, settings: dict):
    """Open an iron condor at credit $1.40, mark to $0.60 (50%+ profit), exit at target."""
    db_path = str(fresh_db)
    settings["paths"]["db_path"] = db_path

    # 1. Initial snapshot — opens with credit $1.40
    #    Iron condor net mid:
    #    -90P + 85P - 100C + 105C
    #    = -3.20 + 1.80 - 3.20 + 1.80
    #    = -2.80
    #    (we receive $2.80 — wait, that's not the same as earlier example)
    #    Let me recompute: SELL 90P (we get +3.20), BUY 85P (we pay 1.80),
    #    SELL 100C (we get 3.20), BUY 105C (we pay 1.80)
    #    Net cash flow: +3.20 - 1.80 + 3.20 - 1.80 = +2.80 (credit)
    #    The combo_mid function uses: BUY = +1, SELL = -1
    #    sum = -3.20 + 1.80 - 3.20 + 1.80 = -2.80 (negative = credit)
    write_chain(snapshot_dir, {
        (90, "P"): 3.20,
        (85, "P"): 1.80,
        (100, "C"): 3.20,
        (105, "C"): 1.80,
    })

    # 2. Insert opening recommendation
    rec_id = insert_recommendation(db_path, {
        "ts": datetime.now(),
        "source": "test",
        "strategy_id": "iron_condor_range_lo",
        "thesis": "test iron condor",
        "structure": "iron_condor",
        "legs": make_iron_condor_legs(),
        "size_units": 1,
        "target_debit": -2.80,        # negative = credit
        "max_loss": 1500,
        "max_profit": 2800,
        "expected_value": 0.0,
        "expiry_date": (date.today() + timedelta(days=20)).isoformat(),
        "confidence": 0.6,
        "status": "pending",
    })

    # 3. Run trader_daemon → paper fill
    trader = TraderDaemon(settings, db_path, str(snapshot_dir))
    trader.tick()

    positions = list_open_positions_full(db_path)
    assert len(positions) == 1
    pos = positions[0]
    assert pos["structure"] == "iron_condor"
    assert pos["qty"] == 1
    assert abs(float(pos["open_debit"]) - (-2.80)) < 0.01

    # 4. Mark to a profitable state — credit shrinks toward 0
    #    Sell 90P at 1.40, Buy 85P at 0.80, Sell 100C at 1.40, Buy 105C at 0.80
    #    Net mid = -1.40 + 0.80 - 1.40 + 0.80 = -1.20
    #    Profit = (-1.20 - (-2.80)) * 1 * 1000 = +$1,600 (well over 50%)
    write_chain(snapshot_dir, {
        (90, "P"): 1.40,
        (85, "P"): 0.80,
        (100, "C"): 1.40,
        (105, "C"): 0.80,
    })

    pm = PositionManager(settings, db_path, str(snapshot_dir))
    pm.tick()

    # Position should now be in 'closing' status (closing rec emitted)
    conn = get_conn(db_path)
    pos_row = conn.execute("SELECT status FROM positions WHERE id = ?", [pos["id"]]).fetchone()
    assert pos_row[0] == "closing"

    closing_rec = conn.execute(
        "SELECT id, source, status FROM recommendations WHERE source='position_manager'"
    ).fetchone()
    assert closing_rec is not None
    assert closing_rec[2] == "pending"

    # 5. Run trader again to process the close
    trader.tick()

    # Position closed, realized P&L should be ≈ +$1,600
    pos_row = conn.execute(
        "SELECT status, close_credit, realized_pnl, exit_reason FROM positions WHERE id = ?",
        [pos["id"]],
    ).fetchone()
    assert pos_row[0] == "closed"
    assert abs(float(pos_row[2]) - 1600.0) < 1.0
    assert pos_row[3] == "target"

    # Daily P&L on cash row should reflect the gain
    cash = get_current_cash(db_path)
    assert abs(float(cash["daily_pnl"]) - 1600.0) < 1.0
    assert abs(float(cash["current_balance"]) - 21600.0) < 1.0


def test_paper_flow_size_check_rejects_oversized_position(fresh_db: Path, snapshot_dir: Path, settings: dict):
    """A recommendation with max_loss > 10% of starting capital should be rejected."""
    db_path = str(fresh_db)
    settings["paths"]["db_path"] = db_path

    write_chain(snapshot_dir, {
        (90, "P"): 3.20, (85, "P"): 1.80,
        (100, "C"): 3.20, (105, "C"): 1.80,
    })

    rec_id = insert_recommendation(db_path, {
        "ts": datetime.now(),
        "source": "test",
        "strategy_id": "iron_condor_range_lo",
        "thesis": "oversized",
        "structure": "iron_condor",
        "legs": make_iron_condor_legs(),
        "size_units": 1,
        "target_debit": -2.80,
        "max_loss": 5000,           # 25% of $20k → should be rejected
        "max_profit": 8000,
        "expected_value": 0.0,
        "expiry_date": (date.today() + timedelta(days=20)).isoformat(),
        "confidence": 0.6,
        "status": "pending",
    })

    trader = TraderDaemon(settings, db_path, str(snapshot_dir))
    trader.tick()

    conn = get_conn(db_path)
    rec_row = conn.execute(
        "SELECT status, rejection_reason FROM recommendations WHERE id = ?", [rec_id]
    ).fetchone()
    assert rec_row[0] == "rejected"
    assert "Single position" in rec_row[1] or "exceeds" in rec_row[1]

    positions = list_open_positions_full(db_path)
    assert positions == []


def test_paper_flow_dte_policy_blocks_short_dte_credit_in_high_vol(fresh_db: Path, snapshot_dir: Path, settings: dict):
    """A 7-DTE iron condor with snapshot IV>80% should be blocked by DTE policy."""
    db_path = str(fresh_db)
    settings["paths"]["db_path"] = db_path

    # Write snapshot with high IV across the chain
    rows = [make_fut_row("CLM6", 95.0)]
    for k, r, mid in [
        (90, "P", 3.20), (85, "P", 1.80),
        (100, "C", 3.20), (105, "C", 1.80),
    ]:
        row = make_fop_row(k, r, mid)
        row["iv"] = 0.95   # 95% IV
        rows.append(row)
    write_snapshot(snapshot_dir, rows)

    rec_id = insert_recommendation(db_path, {
        "ts": datetime.now(),
        "source": "test",
        "strategy_id": "iron_condor_range_lo",
        "thesis": "high vol short dte test",
        "structure": "iron_condor",
        "legs": make_iron_condor_legs(),
        "size_units": 1,
        "target_debit": -2.80,
        "max_loss": 1500,
        "max_profit": 2800,
        "expected_value": 0.0,
        "expiry_date": (date.today() + timedelta(days=7)).isoformat(),
        "confidence": 0.6,
        "status": "pending",
    })

    trader = TraderDaemon(settings, db_path, str(snapshot_dir))
    trader.tick()

    conn = get_conn(db_path)
    rec_row = conn.execute(
        "SELECT status, rejection_reason FROM recommendations WHERE id = ?", [rec_id]
    ).fetchone()
    assert rec_row[0] == "rejected"
    assert "DTE" in rec_row[1]
