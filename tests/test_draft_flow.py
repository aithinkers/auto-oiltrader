"""Regression tests for draft-mode approval flow and mode-aware strategy loading."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from core.db import (
    approve_draft_recommendation,
    close_cached,
    get_conn,
    init_schema,
    reject_draft_recommendation,
    transaction,
)
from daemons.trader_daemon import TraderDaemon


def _expiry_str(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y%m%d")


def make_chain(spot: float = 95.0, dte: int = 14, iv: float = 0.55) -> pd.DataFrame:
    """Build a synthetic LO chain centered on `spot`."""
    expiry = _expiry_str(dte)
    rows = []
    strikes = [round(spot - 15 + i, 1) for i in range(31)]
    for k in strikes:
        moneyness = (k - spot) / spot
        call_delta = max(0.02, min(0.98, 0.5 + moneyness * 4))
        put_delta = -max(0.02, min(0.98, 0.5 - moneyness * 4))

        time_value = max(0.50, 2.0 - abs(moneyness) * 8)
        call_intrinsic = max(0.0, spot - k)
        put_intrinsic = max(0.0, k - spot)
        call_mid = call_intrinsic + time_value
        put_mid = put_intrinsic + time_value

        rows.append({
            "ts": datetime.now(),
            "sec_type": "FOP",
            "trading_class": "LO",
            "expiry": expiry,
            "strike": float(k),
            "right": "C",
            "local_symbol": "",
            "underlying_local_symbol": "CLM6",
            "bid": call_mid - 0.02,
            "ask": call_mid + 0.02,
            "mid": call_mid,
            "last": call_mid,
            "volume": 0,
            "open_interest": 100,
            "iv": iv,
            "delta": call_delta,
            "gamma": 0.01,
            "vega": 0.10,
            "theta": -0.02,
            "underlying_last": spot,
        })
        rows.append({
            "ts": datetime.now(),
            "sec_type": "FOP",
            "trading_class": "LO",
            "expiry": expiry,
            "strike": float(k),
            "right": "P",
            "local_symbol": "",
            "underlying_local_symbol": "CLM6",
            "bid": put_mid - 0.02,
            "ask": put_mid + 0.02,
            "mid": put_mid,
            "last": put_mid,
            "volume": 0,
            "open_interest": 100,
            "iv": iv,
            "delta": put_delta,
            "gamma": 0.01,
            "vega": 0.10,
            "theta": -0.02,
            "underlying_last": spot,
        })
    return pd.DataFrame(rows)


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
    close_cached(str(db))


@pytest.fixture
def settings(tmp_path: Path) -> dict:
    return {
        "stream": {"snapshot_interval_sec": 1},
        "paths": {
            "snapshot_dir": str(tmp_path / "snapshots"),
            "db_path": "",
        },
    }


def write_strategy_config(path: Path, tier: str) -> None:
    path.write_text(
        f"""
strategies:
  - id: bull_put_credit_test
    name: "Bull Put Credit"
    class: strategies.bull_put_credit.BullPutCredit
    tier: {tier}
    enabled: true
    params:
      trading_class: LO
      target_dte_min: 7
      target_dte_max: 21
      wing_offset: 5
      vol_filter_min_iv: 0.30
      min_credit_pct_of_width: 0.05
      default_qty: 1
      max_concurrent: 1
      short_put_delta: -0.25
"""
    )


def set_mode(db_path: str, mode: str) -> None:
    from core.db import utc_now_iso

    with transaction(db_path) as conn:
        cash_row = conn.execute("SELECT * FROM cash ORDER BY ts DESC LIMIT 1").fetchone()
        cash_dict = {k: cash_row[k] for k in cash_row.keys()}
        cash_dict["mode"] = mode
        cash_dict["ts"] = utc_now_iso()
        cash_dict["notes"] = f"test mode change to {mode}"
        col_names = ", ".join(cash_dict.keys())
        placeholders = ", ".join(["?"] * len(cash_dict))
        conn.execute(
            f"INSERT INTO cash ({col_names}) VALUES ({placeholders})",
            list(cash_dict.values()),
        )


def stage_one_draft_order(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: Path,
    settings: dict,
    tmp_path: Path,
) -> tuple[str, TraderDaemon]:
    db_path = str(fresh_db)
    settings["paths"]["db_path"] = db_path
    set_mode(db_path, "draft")
    strategies_path = tmp_path / "strategies_draft.yaml"
    write_strategy_config(strategies_path, tier="draft")
    chain = make_chain()
    monkeypatch.setattr("daemons.trader_daemon.latest_snapshot", lambda _: chain.copy())
    trader = TraderDaemon(settings, db_path, str(tmp_path / "snapshots"), str(strategies_path))
    trader.tick()
    return db_path, trader


def test_trader_daemon_reloads_strategies_after_mode_change(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: Path,
    settings: dict,
    tmp_path: Path,
):
    db_path = str(fresh_db)
    settings["paths"]["db_path"] = db_path
    strategies_path = tmp_path / "strategies_paper.yaml"
    write_strategy_config(strategies_path, tier="paper")

    chain = make_chain()
    monkeypatch.setattr("daemons.trader_daemon.latest_snapshot", lambda _: chain.copy())

    trader = TraderDaemon(settings, db_path, str(tmp_path / "snapshots"), str(strategies_path))
    trader.load_strategies()
    assert trader._loaded_mode == "paper"
    assert [s.id for s in trader.strategies] == ["bull_put_credit_test"]

    set_mode(db_path, "draft")
    trader.tick()

    assert trader._loaded_mode == "draft"
    assert trader.strategies == []

    conn = get_conn(db_path)
    rec_count = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
    order_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert rec_count == 0
    assert order_count == 0


def test_draft_staging_stays_pending_and_respects_max_concurrent(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: Path,
    settings: dict,
    tmp_path: Path,
):
    db_path, trader = stage_one_draft_order(monkeypatch, fresh_db, settings, tmp_path)

    for _ in range(3):
        trader.tick()

    conn = get_conn(db_path)
    rec_rows = conn.execute(
        "SELECT id, status, approved_by FROM recommendations ORDER BY id"
    ).fetchall()
    order_rows = conn.execute(
        "SELECT recommendation_id, status FROM orders ORDER BY id"
    ).fetchall()

    assert len(rec_rows) == 1
    assert rec_rows[0]["status"] == "pending"
    assert rec_rows[0]["approved_by"] is None

    assert len(order_rows) == 1
    assert order_rows[0]["status"] == "draft"
    assert order_rows[0]["recommendation_id"] == rec_rows[0]["id"]


def test_approve_draft_recommendation_marks_human_approval(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: Path,
    settings: dict,
    tmp_path: Path,
):
    db_path, _ = stage_one_draft_order(monkeypatch, fresh_db, settings, tmp_path)
    conn = get_conn(db_path)
    rec_id = int(conn.execute("SELECT id FROM recommendations").fetchone()[0])

    ok, message = approve_draft_recommendation(db_path, rec_id, "tester")
    assert ok, message

    rec_row = conn.execute(
        "SELECT status, approved_by, approved_at FROM recommendations WHERE id = ?",
        [rec_id],
    ).fetchone()
    order_row = conn.execute(
        "SELECT status, notes FROM orders WHERE recommendation_id = ?",
        [rec_id],
    ).fetchone()

    assert rec_row["status"] == "approved"
    assert rec_row["approved_by"] == "tester"
    assert rec_row["approved_at"] is not None
    assert order_row["status"] == "draft"
    assert "approved by tester" in (order_row["notes"] or "")


def test_reject_draft_recommendation_cancels_order(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: Path,
    settings: dict,
    tmp_path: Path,
):
    db_path, _ = stage_one_draft_order(monkeypatch, fresh_db, settings, tmp_path)
    conn = get_conn(db_path)
    rec_id = int(conn.execute("SELECT id FROM recommendations").fetchone()[0])

    ok, message = reject_draft_recommendation(db_path, rec_id, "tester", "not today")
    assert ok, message

    rec_row = conn.execute(
        "SELECT status, approved_at, rejection_reason FROM recommendations WHERE id = ?",
        [rec_id],
    ).fetchone()
    order_row = conn.execute(
        "SELECT status, notes FROM orders WHERE recommendation_id = ?",
        [rec_id],
    ).fetchone()

    assert rec_row["status"] == "rejected"
    assert rec_row["approved_at"] is None
    assert rec_row["rejection_reason"] == "not today"
    assert order_row["status"] == "cancelled"
    assert "not today" in (order_row["notes"] or "")
