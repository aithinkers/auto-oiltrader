"""Loads strategy classes from config/strategies.yaml.

The trader_daemon calls `load_enabled_strategies()` once at startup and gets
back a list of instantiated Strategy objects ready for `evaluate()` calls.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

import yaml

from strategies.base import Strategy


# Tiers that are allowed to generate executable recommendations in each mode.
# experimental and shadow are never executable — they only track hypothetical P&L.
_EXECUTABLE_TIERS: dict[str, set[str]] = {
    "paper": {"paper", "draft", "live"},
    "draft": {"draft", "live"},
    "live":  {"live"},
    "halt":  set(),  # nothing runs in halt
}


def load_enabled_strategies(
    yaml_path: str | Path,
    mode: str = "paper",
) -> list[Strategy]:
    """Load and instantiate strategies that are enabled AND whose tier allows
    execution in the given mode.

    Strategies in experimental/shadow tiers are skipped — they should only be
    tracked for hypothetical P&L, not generate real recommendations.
    """
    path = Path(yaml_path)
    if not path.exists():
        logging.warning("Strategy config not found: %s", path)
        return []

    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    allowed_tiers = _EXECUTABLE_TIERS.get(mode, set())
    out: list[Strategy] = []
    for entry in cfg.get("strategies", []):
        if not entry.get("enabled", False):
            continue
        tier = entry.get("tier", "experimental")
        if tier not in allowed_tiers:
            logging.info(
                "Skipping strategy %s (tier=%s not executable in mode=%s)",
                entry.get("id"), tier, mode,
            )
            continue
        try:
            klass = _import_class(entry["class"])
            instance = klass(
                id=entry["id"],
                name=entry["name"],
                tier=tier,
                enabled=True,
                params=entry.get("params", {}),
            )
            out.append(instance)
            logging.info("Loaded strategy %s (tier=%s)", entry["id"], tier)
        except Exception as e:
            logging.exception("Failed to load strategy %s: %s", entry.get("id"), e)
    return out


def _import_class(dotted: str):
    """Import 'pkg.module.Class' and return the class object."""
    module_path, class_name = dotted.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)
