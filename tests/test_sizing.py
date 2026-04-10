"""Tests for core/sizing.py — capital boundary checks."""

import os
import tempfile
from pathlib import Path

import pytest

from core.db import init_schema
from core.sizing import CapitalLimits, check_proposed_order


@pytest.fixture
def fresh_db(tmp_path):
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
    yield str(db)
    # Clean up cached connections from this test
    from core.db import close_cached
    close_cached(str(db))


def test_proposed_order_within_limits(fresh_db):
    result = check_proposed_order(fresh_db, proposed_max_loss=500)
    assert result.allowed
    assert "ok" in result.reason


def test_proposed_order_exceeds_position_limit(fresh_db):
    # Default seed has $20k starting capital, max_position = 10% = $2000
    result = check_proposed_order(fresh_db, proposed_max_loss=3000)
    assert not result.allowed
    assert "Single position" in result.reason


def test_halt_mode_blocks(fresh_db):
    import sqlite3
    conn = sqlite3.connect(fresh_db, isolation_level=None)
    try:
        conn.execute("UPDATE cash SET mode='halt' WHERE 1=1")
    finally:
        conn.close()
    # Drop any cached connection so the next read sees the new mode
    from core.db import close_cached
    close_cached(fresh_db)
    result = check_proposed_order(fresh_db, proposed_max_loss=100)
    assert not result.allowed
    assert "halt" in result.reason
