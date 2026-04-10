"""Tests for core/summarizer.py and daemons/summarizer.py."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.db import init_schema, insert_position, insert_recommendation, write_commentary
from core.summarizer import build_summary
from daemons.summarizer import _next_aligned_tick, persist_summary, write_md_file


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
def empty_snapshot_dir(tmp_path: Path) -> Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


# ─── build_summary basics ────────────────────────────────────────────
def test_summary_with_empty_db(fresh_db: Path, empty_snapshot_dir: Path):
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    assert s.headline
    assert "Summary" in s.body_md
    assert s.metrics["mode"] == "paper"
    assert s.metrics["open_position_count"] == 0
    assert s.metrics["new_positions_count"] == 0
    assert s.metrics["closed_positions_count"] == 0
    assert not s.is_important


def test_summary_with_open_position(fresh_db: Path, empty_snapshot_dir: Path):
    insert_position(fresh_db, {
        "ts_opened": datetime.now(),
        "structure": "iron_condor",
        "legs": [],
        "qty": 1,
        "open_debit": -2.50,
        "status": "open",
        "mode": "paper",
        "strategy_id": "iron_condor_range_lo",
    })
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    assert s.metrics["open_position_count"] == 1
    assert s.metrics["new_positions_count"] == 1
    assert "iron_condor" in s.body_md
    assert s.is_important  # new position in window


def test_summary_with_closed_position(fresh_db: Path, empty_snapshot_dir: Path):
    insert_position(fresh_db, {
        "ts_opened": (datetime.now() - timedelta(minutes=30)).isoformat(),
        "ts_closed": datetime.now().isoformat(),
        "structure": "bull_put_credit_spread",
        "legs": [],
        "qty": 1,
        "open_debit": -1.40,
        "close_credit": -0.50,
        "realized_pnl": 900,
        "status": "closed",
        "exit_reason": "target",
        "mode": "paper",
        "strategy_id": "bull_put_credit_lo",
    })
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    assert s.metrics["closed_positions_count"] == 1
    assert s.metrics["winners_in_window"] == 1
    assert s.metrics["realized_pnl_window"] == 900
    assert "✅" in s.body_md
    assert s.is_important


def test_summary_with_rejected_recommendation(fresh_db: Path, empty_snapshot_dir: Path):
    rec = {
        "ts": datetime.now(),
        "source": "strategy:test",
        "strategy_id": "test",
        "thesis": "test",
        "structure": "iron_condor",
        "legs": [],
        "size_units": 1,
        "target_debit": -2.50,
        "max_loss": 5000,
        "max_profit": 2500,
        "expected_value": 0,
        "expiry_date": (datetime.now() + timedelta(days=20)).date().isoformat(),
        "confidence": 0.5,
        "status": "rejected",
        "rejection_reason": "Single position exceeds 10% of starting capital",
    }
    insert_recommendation(fresh_db, rec)
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    assert s.metrics["rec_counts_window"].get("rejected") == 1
    assert "Single position" in s.body_md


def test_summary_with_alert(fresh_db: Path, empty_snapshot_dir: Path):
    write_commentary(fresh_db, "test alert message", level="alert", topic="trade")
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    assert s.metrics["alerts_count"] == 1
    assert "🚨" in s.body_md
    assert s.is_important


def test_summary_window_excludes_old_events(fresh_db: Path, empty_snapshot_dir: Path):
    # Insert a position with ts_opened 3 hours ago
    insert_position(fresh_db, {
        "ts_opened": (datetime.now() - timedelta(hours=3)).isoformat(),
        "structure": "iron_condor",
        "legs": [],
        "qty": 1,
        "open_debit": -2.50,
        "status": "open",
        "mode": "paper",
    })
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    assert s.metrics["open_position_count"] == 1   # still open
    assert s.metrics["new_positions_count"] == 0   # opened outside window
    assert not s.is_important                       # nothing happened in window


# ─── _next_aligned_tick ──────────────────────────────────────────────
def test_aligned_tick_to_next_hour():
    now = datetime(2026, 4, 9, 14, 23, 17)
    nxt = _next_aligned_tick(3600, True, now)
    assert nxt == datetime(2026, 4, 9, 15, 0, 0)


def test_aligned_tick_at_top_of_hour():
    now = datetime(2026, 4, 9, 14, 0, 0)
    nxt = _next_aligned_tick(3600, True, now)
    assert nxt == datetime(2026, 4, 9, 15, 0, 0)


def test_unaligned_tick_just_adds_interval():
    now = datetime(2026, 4, 9, 14, 23, 17)
    nxt = _next_aligned_tick(3600, False, now)
    assert nxt == datetime(2026, 4, 9, 15, 23, 17)


def test_aligned_tick_15min_interval():
    now = datetime(2026, 4, 9, 14, 23, 17)
    nxt = _next_aligned_tick(900, True, now)
    assert nxt == datetime(2026, 4, 9, 14, 30, 0)


# ─── persist_summary + write_md_file ─────────────────────────────────
def test_persist_summary(fresh_db: Path, empty_snapshot_dir: Path):
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    sid = persist_summary(str(fresh_db), s, push_target=None)
    assert sid > 0
    import sqlite3
    conn = sqlite3.connect(str(fresh_db))
    row = conn.execute("SELECT headline, pushed FROM summaries WHERE id = ?", [sid]).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == s.headline
    assert row[1] == 0


def test_write_md_file(tmp_path: Path, fresh_db: Path, empty_snapshot_dir: Path):
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    out = write_md_file(tmp_path / "summaries", s)
    assert out.exists()
    body = out.read_text()
    assert "# Summary" in body
