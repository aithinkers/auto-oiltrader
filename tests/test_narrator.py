"""Tests for agents/narrator.py — uses monkeypatched invoke() to avoid real LLM calls."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agents import narrator
from core.db import init_schema, insert_position, insert_recommendation
from core.summarizer import build_summary


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


@pytest.fixture
def stub_invoke(monkeypatch):
    """Replace agents.runtime.invoke so tests don't actually call Claude."""
    captured = {}

    def fake_invoke(db_path, agent_name, system, prompt, model=None, max_tokens=2048,
                    monthly_budget=200.0, inputs_for_log=None):
        captured["db_path"] = db_path
        captured["agent_name"] = agent_name
        captured["system"] = system
        captured["prompt"] = prompt
        captured["model"] = model
        # Return a deterministic narrative-shaped response
        text = "STUB NARRATIVE: quiet hour, no positions, futures drift -0.5%."
        meta = {"tokens_in": 50, "tokens_out": 12, "model": model or "stub", "cost": 0.0001, "decision_id": 1}
        return text, meta

    monkeypatch.setattr("agents.narrator.invoke", fake_invoke)
    return captured


# ---------------------------------------------------------------------------
# narrate_summary
# ---------------------------------------------------------------------------
def test_narrate_summary_returns_text(fresh_db, empty_snapshot_dir, stub_invoke):
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    text = narrator.narrate_summary(s, str(fresh_db))
    assert text is not None
    assert "STUB NARRATIVE" in text
    assert stub_invoke["agent_name"] == "narrator.narrate_summary"
    assert "json" in stub_invoke["prompt"].lower()


def test_narrate_summary_uses_haiku_by_default(fresh_db, empty_snapshot_dir, stub_invoke):
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    narrator.narrate_summary(s, str(fresh_db))
    # The skill frontmatter says claude-haiku-4-5-20251001
    assert "haiku" in stub_invoke["model"].lower()


def test_narrate_summary_includes_position_info(fresh_db, empty_snapshot_dir, stub_invoke):
    insert_position(fresh_db, {
        "ts_opened": datetime.now().isoformat(),
        "structure": "iron_condor",
        "legs": [],
        "qty": 1,
        "open_debit": -2.50,
        "status": "open",
        "mode": "paper",
        "strategy_id": "iron_condor_range_lo",
    })
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    narrator.narrate_summary(s, str(fresh_db))
    assert '"open_position_count": 1' in stub_invoke["prompt"]
    assert '"new_positions_count": 1' in stub_invoke["prompt"]


def test_narrate_summary_returns_none_on_failure(fresh_db, empty_snapshot_dir, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("LLM call failed")
    monkeypatch.setattr("agents.narrator.invoke", boom)
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    text = narrator.narrate_summary(s, str(fresh_db))
    assert text is None


def test_narrate_summary_returns_none_on_budget_exceeded(fresh_db, empty_snapshot_dir, monkeypatch):
    from agents.runtime import BudgetExceeded
    def over_budget(*a, **kw):
        raise BudgetExceeded("monthly cap")
    monkeypatch.setattr("agents.narrator.invoke", over_budget)
    s = build_summary(fresh_db, empty_snapshot_dir, window_hours=1)
    text = narrator.narrate_summary(s, str(fresh_db))
    assert text is None


# ---------------------------------------------------------------------------
# analyze_trade
# ---------------------------------------------------------------------------
def test_analyze_trade_returns_none_for_unknown_position(fresh_db, stub_invoke):
    text = narrator.analyze_trade(99999, str(fresh_db))
    assert text is None


def test_analyze_trade_loads_full_context(fresh_db, stub_invoke):
    rec_id = insert_recommendation(fresh_db, {
        "ts": datetime.now(),
        "source": "test",
        "strategy_id": "iron_condor_range_lo",
        "thesis": "test thesis: vol rich, short premium",
        "structure": "iron_condor",
        "legs": [{"trading_class": "LO", "expiry": "20260616", "strike": 90, "right": "P", "ratio": 1, "action": "SELL"}],
        "size_units": 1,
        "target_debit": -1.40,
        "max_loss": 1500,
        "max_profit": 1400,
        "expected_value": 0,
        "expiry_date": (datetime.now() + timedelta(days=20)).date().isoformat(),
        "confidence": 0.55,
        "status": "executed",
    })
    pid = insert_position(fresh_db, {
        "ts_opened": datetime.now().isoformat(),
        "recommendation_id": rec_id,
        "strategy_id": "iron_condor_range_lo",
        "structure": "iron_condor",
        "legs": [{"trading_class": "LO", "expiry": "20260616", "strike": 90, "right": "P", "ratio": 1, "action": "SELL"}],
        "qty": 1,
        "open_debit": -1.40,
        "status": "open",
        "mode": "paper",
    })
    text = narrator.analyze_trade(pid, str(fresh_db))
    assert text is not None
    assert "STUB NARRATIVE" in text
    assert stub_invoke["agent_name"] == f"narrator.analyze_trade:{pid}"
    # Confirm the prompt includes the position thesis
    assert "test thesis" in stub_invoke["prompt"]
    assert "iron_condor" in stub_invoke["prompt"]


def test_analyze_trade_uses_sonnet_by_default(fresh_db, stub_invoke):
    rec_id = insert_recommendation(fresh_db, {
        "ts": datetime.now(),
        "source": "test",
        "strategy_id": "iron_condor_range_lo",
        "thesis": "x",
        "structure": "iron_condor",
        "legs": [],
        "size_units": 1,
        "target_debit": -1.40,
        "max_loss": 1500,
        "max_profit": 1400,
        "expected_value": 0,
        "expiry_date": (datetime.now() + timedelta(days=20)).date().isoformat(),
        "confidence": 0.55,
        "status": "executed",
    })
    pid = insert_position(fresh_db, {
        "ts_opened": datetime.now().isoformat(),
        "recommendation_id": rec_id,
        "strategy_id": "iron_condor_range_lo",
        "structure": "iron_condor",
        "legs": [],
        "qty": 1,
        "open_debit": -1.40,
        "status": "open",
        "mode": "paper",
    })
    narrator.analyze_trade(pid, str(fresh_db))
    assert "sonnet" in stub_invoke["model"].lower()


# ---------------------------------------------------------------------------
# Skill loading
# ---------------------------------------------------------------------------
def test_load_skill_parses_frontmatter():
    body, fm = narrator._load_skill("narrate_summary")
    assert "model" in fm
    assert "haiku" in fm["model"]
    assert len(body) > 100


def test_load_skill_missing_raises():
    with pytest.raises(FileNotFoundError):
        narrator._load_skill("nonexistent_skill_xyz")
