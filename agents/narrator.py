"""Narrator agent — LLM-powered narrative analysis.

Two public functions:

  narrate_summary(summary, db_path, ...) -> str
      Takes a rules-based Summary (from core.summarizer.build_summary) and
      returns a 3-5 sentence narrative paragraph explaining what it MEANS.
      Used by the summarizer worker when summarizer.include_llm_narrative=true.

  analyze_trade(position_id, db_path, ...) -> str
      Takes a position_id and returns a detailed markdown analysis of the
      trade (thesis, state, risk, recommended action). Used by
      `tradectl analyze <id>` and the dashboard.

Both load their prompts from skills/*.md so the operator can tweak the tone
without touching code.

Cost: Haiku for narrate_summary (~$0.004/hour), Sonnet for analyze_trade
(~$0.02/call). Both respect the monthly LLM budget cap.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from agents.runtime import BudgetExceeded, invoke
from core.db import get_conn
from core.summarizer import Summary


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------
def _load_skill(skill_name: str) -> tuple[str, dict]:
    """Load a skill markdown file and return (system_prompt, frontmatter)."""
    path = Path(__file__).parent.parent / "skills" / f"{skill_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {path}")
    text = path.read_text()

    # Crude frontmatter parser — enough for our needs
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            fm_text = text[4:end]
            for line in fm_text.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    frontmatter[k.strip()] = v.strip()
            body = text[end + 5:]
    return body.strip(), frontmatter


# ---------------------------------------------------------------------------
# narrate_summary
# ---------------------------------------------------------------------------
def _compact_summary_for_llm(summary: Summary, max_positions: int = 10, max_alerts: int = 5) -> dict:
    """Build the JSON input for the narrate_summary skill. Keeps tokens small."""
    m = summary.metrics

    futures = m.get("futures_moves", {})
    # Round aggressively to save tokens and noise
    compact_futures = {
        sym: {
            "start": round(v["start"], 2),
            "end": round(v["end"], 2),
            "change": round(v["change"], 2),
            "pct": round(v["pct"], 2),
        }
        for sym, v in futures.items()
    }

    return {
        "window_hours": (summary.period_end - summary.period_start).total_seconds() / 3600,
        "mode": m.get("mode"),
        "balance": round(m.get("current_balance", 0), 2),
        "starting": round(m.get("starting_capital", 0), 2),
        "daily_pnl": round(m.get("daily_pnl", 0), 2),
        "daily_loss_halt": round(m.get("daily_loss_halt", 0), 2),
        "futures_moves": compact_futures,
        "open_position_count": m.get("open_position_count", 0),
        "total_unrealized_pnl": round(m.get("total_unrealized_pnl", 0), 0),
        "new_positions_count": m.get("new_positions_count", 0),
        "closed_positions_count": m.get("closed_positions_count", 0),
        "realized_pnl_window": round(m.get("realized_pnl_window", 0), 0),
        "winners_in_window": m.get("winners_in_window", 0),
        "losers_in_window": m.get("losers_in_window", 0),
        "recs": m.get("rec_counts_window", {}),
        "alerts_count": m.get("alerts_count", 0),
        "costs": {k: round(v, 4) for k, v in m.get("costs_window", {}).items()},
    }


def narrate_summary(
    summary: Summary,
    db_path: str,
    model: str | None = None,
    monthly_budget: float = 200.0,
) -> str | None:
    """Call Claude to produce a narrative paragraph for a Summary.

    Returns the narrative text, or None if the LLM call fails or budget
    exceeded. The caller should fall back to the rules-only summary.
    """
    try:
        system_prompt, frontmatter = _load_skill("narrate_summary")
    except FileNotFoundError as e:
        logging.warning("narrate_summary skill missing: %s", e)
        return None

    use_model = model or frontmatter.get("model", "claude-haiku-4-5-20251001")
    compact = _compact_summary_for_llm(summary)

    user_prompt = (
        "Here is the structured summary of the last window:\n\n"
        + "```json\n"
        + json.dumps(compact, indent=2, default=str)
        + "\n```\n\n"
        + "Write the narrative paragraph as instructed."
    )

    try:
        text, _meta = invoke(
            db_path=db_path,
            agent_name="narrator.narrate_summary",
            system=system_prompt,
            prompt=user_prompt,
            model=use_model,
            max_tokens=512,
            monthly_budget=monthly_budget,
        )
    except BudgetExceeded as e:
        logging.warning("narrate_summary skipped: %s", e)
        return None
    except Exception as e:
        logging.exception("narrate_summary LLM call failed: %s", e)
        return None

    return text.strip() if text else None


# ---------------------------------------------------------------------------
# analyze_trade
# ---------------------------------------------------------------------------
def _load_trade_context(db_path: str, position_id: int) -> dict | None:
    """Build the JSON input for the analyze_trade skill."""
    conn = get_conn(db_path)

    # Position
    row = conn.execute(
        """
        SELECT id, strategy_id, structure, qty, legs, open_debit, close_credit,
               realized_pnl, status, ts_opened, ts_closed, exit_reason, mode,
               recommendation_id
        FROM positions WHERE id = ?
        """,
        [position_id],
    ).fetchone()
    if row is None:
        return None
    pos = {k: row[k] for k in row.keys()}
    try:
        pos["legs"] = json.loads(pos["legs"]) if isinstance(pos["legs"], str) else pos["legs"]
    except (TypeError, json.JSONDecodeError):
        pos["legs"] = []

    # Original recommendation (for thesis)
    original_rec = None
    if pos.get("recommendation_id"):
        rec_row = conn.execute(
            """
            SELECT id, thesis, confidence, max_loss, max_profit, target_debit,
                   expiry_date, source
            FROM recommendations WHERE id = ?
            """,
            [pos["recommendation_id"]],
        ).fetchone()
        if rec_row:
            original_rec = {k: rec_row[k] for k in rec_row.keys()}

    # Mark history
    mark_rows = conn.execute(
        """
        SELECT ts, mark, unrealized_pnl, delta, gamma, vega, theta, underlying_last
        FROM position_marks WHERE position_id = ? ORDER BY ts
        """,
        [position_id],
    ).fetchall()
    marks = [{k: r[k] for k in r.keys()} for r in mark_rows]

    # Strategy params from the config yaml (not the DB — yaml is the source of truth)
    strategy_info = None
    try:
        import yaml
        yaml_path = Path(__file__).parent.parent / "config" / "strategies.yaml"
        if yaml_path.exists():
            cfg = yaml.safe_load(yaml_path.read_text()) or {}
            for s in cfg.get("strategies", []):
                if s.get("id") == pos.get("strategy_id"):
                    strategy_info = {
                        "id": s["id"],
                        "name": s["name"],
                        "tier": s["tier"],
                        "params": s.get("params", {}),
                    }
                    break
    except Exception:
        pass

    # DTE
    dte_remaining = None
    time_since_open_hours = None
    try:
        if pos["legs"]:
            first_exp = pos["legs"][0].get("expiry")
            if first_exp:
                exp_dt = datetime.strptime(str(first_exp), "%Y%m%d")
                dte_remaining = (exp_dt - datetime.now()).days
        if pos.get("ts_opened"):
            open_dt = datetime.fromisoformat(pos["ts_opened"])
            time_since_open_hours = round((datetime.now() - open_dt).total_seconds() / 3600, 1)
    except Exception:
        pass

    # Compact marks — keep only ~20 points
    if len(marks) > 20:
        step = max(1, len(marks) // 20)
        marks = marks[::step]

    return {
        "position": pos,
        "strategy": strategy_info,
        "original_recommendation": original_rec,
        "marks": marks,
        "dte_remaining": dte_remaining,
        "time_since_open_hours": time_since_open_hours,
    }


def analyze_trade(
    position_id: int,
    db_path: str,
    model: str | None = None,
    monthly_budget: float = 200.0,
) -> str | None:
    """Call Claude (Sonnet) to produce a detailed markdown analysis of a trade.

    Returns the markdown text, or None if the position doesn't exist,
    the LLM call fails, or budget exceeded.
    """
    ctx = _load_trade_context(db_path, position_id)
    if ctx is None:
        return None

    try:
        system_prompt, frontmatter = _load_skill("analyze_trade")
    except FileNotFoundError as e:
        logging.warning("analyze_trade skill missing: %s", e)
        return None

    use_model = model or frontmatter.get("model", "claude-sonnet-4-6")

    user_prompt = (
        "Here is the full trade context:\n\n"
        + "```json\n"
        + json.dumps(ctx, indent=2, default=str)
        + "\n```\n\n"
        + "Produce the trade analysis as instructed."
    )

    try:
        text, _meta = invoke(
            db_path=db_path,
            agent_name=f"narrator.analyze_trade:{position_id}",
            system=system_prompt,
            prompt=user_prompt,
            model=use_model,
            max_tokens=1024,
            monthly_budget=monthly_budget,
        )
    except BudgetExceeded as e:
        logging.warning("analyze_trade skipped: %s", e)
        return None
    except Exception as e:
        logging.exception("analyze_trade LLM call failed: %s", e)
        return None

    return text.strip() if text else None
