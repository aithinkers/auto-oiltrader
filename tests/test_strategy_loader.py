"""Tests for core/strategy_loader.py."""

from pathlib import Path

from core.strategy_loader import load_enabled_strategies


def test_loader_reads_real_yaml_paper_mode():
    """In paper mode, only paper-tier strategies load (experimental ones are filtered)."""
    yaml_path = Path(__file__).parent.parent / "config" / "strategies.yaml"
    strategies = load_enabled_strategies(yaml_path, mode="paper")
    # 3 paper-tier strategies: iron_condor, bull_put_credit, bear_call_credit
    assert len(strategies) >= 3
    ids = {s.id for s in strategies}
    assert "iron_condor_range_lo" in ids
    assert "bull_put_credit_lo" in ids
    assert "bear_call_credit_lo" in ids
    # Experimental-tier strategies should NOT load in paper mode
    assert "bull_call_debit_lo" not in ids
    assert "bear_put_debit_lo" not in ids


def test_loader_handles_missing_file(tmp_path):
    bogus = tmp_path / "missing.yaml"
    assert load_enabled_strategies(bogus) == []


def test_loader_skips_disabled(tmp_path):
    yaml_text = """
strategies:
  - id: enabled_one
    name: "Enabled"
    class: strategies.iron_condor_range.IronCondorRange
    tier: paper
    enabled: true
    params: {}
  - id: disabled_one
    name: "Disabled"
    class: strategies.iron_condor_range.IronCondorRange
    tier: paper
    enabled: false
    params: {}
"""
    p = tmp_path / "strats.yaml"
    p.write_text(yaml_text)
    strats = load_enabled_strategies(p)
    assert len(strats) == 1
    assert strats[0].id == "enabled_one"


def test_loader_skips_invalid_class(tmp_path):
    yaml_text = """
strategies:
  - id: bad
    name: "Bad import path"
    class: nonexistent.module.Bad
    tier: paper
    enabled: true
    params: {}
  - id: good
    name: "Good"
    class: strategies.iron_condor_range.IronCondorRange
    tier: paper
    enabled: true
    params: {}
"""
    p = tmp_path / "strats.yaml"
    p.write_text(yaml_text)
    strats = load_enabled_strategies(p)
    assert len(strats) == 1
    assert strats[0].id == "good"
