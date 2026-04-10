"""Periodic summarizer — pure rules, no LLM by default.

Builds a markdown digest of what happened in the last N hours by querying:
  - cash table (mode, balance, daily P&L)
  - positions / position_marks (open + opened/closed in window)
  - recommendations (emitted, executed, rejected w/ reasons)
  - commentary (alerts and warnings)
  - costs (LLM + commission burn)
  - parquet snapshots (futures price changes via DuckDB read_parquet)

The output is a `Summary` object with:
  - headline       : 1-line phone-notification text
  - body_md        : full markdown digest
  - metrics        : structured dict for dashboard charts

This module is pure: it does NOT write to the DB. The daemons/summarizer.py
worker handles persistence + push notifications.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.db import get_conn


@dataclass
class Summary:
    ts: datetime
    period_start: datetime
    period_end: datetime
    headline: str
    body_md: str
    metrics: dict[str, Any] = field(default_factory=dict)
    is_important: bool = False  # used by push_threshold="important_only"


def _q(conn: sqlite3.Connection, sql: str, params: list | tuple = ()) -> list[dict]:
    rows = conn.execute(sql, params).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _q_one(conn: sqlite3.Connection, sql: str, params: list | tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else {k: row[k] for k in row.keys()}


def _scalar(conn: sqlite3.Connection, sql: str, params: list | tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _futures_price_changes(snapshot_dir: str | Path, period_start: datetime, period_end: datetime) -> dict[str, dict]:
    """Compare futures last prices between the snapshot closest to period_start
    and the latest snapshot before period_end."""
    try:
        import duckdb
    except ImportError:
        return {}

    base = Path(snapshot_dir)
    if not base.exists():
        return {}

    glob = str(base / "**" / "*.parquet")
    con = duckdb.connect()
    try:
        # Find earliest and latest ts in window
        bounds = con.execute(
            f"""
            SELECT
                (SELECT MIN(ts) FROM read_parquet('{glob}') WHERE ts >= ?) AS start_ts,
                (SELECT MAX(ts) FROM read_parquet('{glob}') WHERE ts <= ?) AS end_ts
            """,
            [period_start, period_end],
        ).fetchone()
    except Exception:
        return {}

    if not bounds or bounds[0] is None or bounds[1] is None:
        return {}

    start_ts, end_ts = bounds
    try:
        start_rows = con.execute(
            f"SELECT local_symbol, last FROM read_parquet('{glob}') "
            f"WHERE sec_type='FUT' AND ts = ?",
            [start_ts],
        ).fetchall()
        end_rows = con.execute(
            f"SELECT local_symbol, last FROM read_parquet('{glob}') "
            f"WHERE sec_type='FUT' AND ts = ?",
            [end_ts],
        ).fetchall()
    except Exception:
        return {}

    start_map = {r[0]: float(r[1]) for r in start_rows if r[1] is not None and float(r[1]) > 0}
    end_map = {r[0]: float(r[1]) for r in end_rows if r[1] is not None and float(r[1]) > 0}
    out = {}
    for sym in sorted(start_map.keys() | end_map.keys()):
        s = start_map.get(sym)
        e = end_map.get(sym)
        if s and e:
            out[sym] = {"start": s, "end": e, "change": e - s, "pct": (e - s) / s * 100}
    return out


def build_summary(
    db_path: str | Path,
    snapshot_dir: str | Path,
    window_hours: float = 1.0,
    now: datetime | None = None,
) -> Summary:
    """Build a summary of what happened in the last `window_hours` hours."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    period_start = now - timedelta(hours=window_hours)
    period_end = now

    # SQL parameters need to match the format used by core.db.utc_now_iso():
    #   YYYY-MM-DDTHH:MM:SS.fffZ
    def _iso(dt: datetime) -> str:
        d = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    period_start_iso = _iso(period_start)
    period_end_iso = _iso(period_end)

    conn = get_conn(db_path)
    metrics: dict[str, Any] = {}

    # ----- Account snapshot -----
    cash = _q_one(conn, "SELECT * FROM cash ORDER BY ts DESC LIMIT 1")
    if cash:
        metrics["mode"] = cash["mode"]
        metrics["starting_capital"] = float(cash["starting_capital"])
        metrics["current_balance"] = float(cash["current_balance"])
        metrics["daily_pnl"] = float(cash["daily_pnl"])
        metrics["daily_loss_halt"] = float(cash["daily_loss_halt"])

    # ----- Open positions -----
    open_positions = _q(
        conn,
        """
        SELECT p.id, p.strategy_id, p.structure, p.qty, p.open_debit, p.ts_opened,
               (SELECT mark FROM position_marks WHERE position_id = p.id ORDER BY ts DESC LIMIT 1) AS latest_mark,
               (SELECT unrealized_pnl FROM position_marks WHERE position_id = p.id ORDER BY ts DESC LIMIT 1) AS latest_upnl
        FROM positions p
        WHERE p.status = 'open'
        ORDER BY p.ts_opened
        """,
    )
    metrics["open_position_count"] = len(open_positions)
    metrics["total_unrealized_pnl"] = sum(float(p.get("latest_upnl") or 0) for p in open_positions)

    # ----- Positions opened in window -----
    new_positions = _q(
        conn,
        """
        SELECT id, strategy_id, structure, qty, open_debit, ts_opened
        FROM positions
        WHERE ts_opened >= ? AND ts_opened <= ?
        ORDER BY ts_opened
        """,
        [period_start_iso, period_end_iso],
    )
    metrics["new_positions_count"] = len(new_positions)

    # ----- Positions closed in window -----
    closed_positions = _q(
        conn,
        """
        SELECT id, strategy_id, structure, qty, open_debit, close_credit,
               realized_pnl, exit_reason, ts_closed
        FROM positions
        WHERE status = 'closed' AND ts_closed >= ? AND ts_closed <= ?
        ORDER BY ts_closed
        """,
        [period_start_iso, period_end_iso],
    )
    metrics["closed_positions_count"] = len(closed_positions)
    metrics["realized_pnl_window"] = sum(float(p.get("realized_pnl") or 0) for p in closed_positions)
    metrics["winners_in_window"] = sum(1 for p in closed_positions if (p.get("realized_pnl") or 0) > 0)
    metrics["losers_in_window"] = sum(1 for p in closed_positions if (p.get("realized_pnl") or 0) < 0)

    # ----- Recommendations in window -----
    rec_breakdown = _q(
        conn,
        """
        SELECT status, COUNT(*) AS n
        FROM recommendations
        WHERE ts >= ? AND ts <= ?
        GROUP BY status
        """,
        [period_start_iso, period_end_iso],
    )
    rec_counts = {r["status"]: int(r["n"]) for r in rec_breakdown}
    metrics["rec_counts_window"] = rec_counts

    # Top rejection reasons
    top_rejections = _q(
        conn,
        """
        SELECT rejection_reason, COUNT(*) AS n
        FROM recommendations
        WHERE status = 'rejected' AND ts >= ? AND ts <= ?
        GROUP BY rejection_reason
        ORDER BY n DESC
        LIMIT 5
        """,
        [period_start_iso, period_end_iso],
    )

    # ----- Critical commentary in window -----
    alerts = _q(
        conn,
        """
        SELECT ts, level, topic, message
        FROM commentary
        WHERE ts >= ? AND ts <= ? AND level IN ('warn', 'alert', 'critical')
        ORDER BY ts DESC
        LIMIT 20
        """,
        [period_start_iso, period_end_iso],
    )
    metrics["alerts_count"] = len(alerts)

    # ----- Costs in window -----
    cost_window = _q(
        conn,
        """
        SELECT category, SUM(amount) AS total
        FROM costs
        WHERE ts >= ? AND ts <= ?
        GROUP BY category
        """,
        [period_start_iso, period_end_iso],
    )
    cost_map = {r["category"]: float(r["total"] or 0) for r in cost_window}
    metrics["costs_window"] = cost_map

    # ----- Futures price moves -----
    fut_moves = _futures_price_changes(snapshot_dir, period_start, period_end)
    metrics["futures_moves"] = fut_moves

    # ----- Determine importance -----
    is_important = (
        metrics.get("new_positions_count", 0) > 0
        or metrics.get("closed_positions_count", 0) > 0
        or metrics.get("alerts_count", 0) > 0
        or any(c.get("level") == "critical" for c in alerts)
    )

    # ----- Build headline -----
    parts = []
    if cash:
        parts.append(f"{cash['mode']}")
        parts.append(f"P&L ${float(cash['daily_pnl']):+,.0f}")
    if metrics.get("open_position_count"):
        parts.append(f"{metrics['open_position_count']} open")
    if metrics.get("new_positions_count"):
        parts.append(f"+{metrics['new_positions_count']} new")
    if metrics.get("closed_positions_count"):
        parts.append(
            f"{metrics['closed_positions_count']} closed "
            f"({metrics['winners_in_window']}W/{metrics['losers_in_window']}L)"
        )
    if metrics.get("alerts_count"):
        parts.append(f"⚠{metrics['alerts_count']}")
    headline = " · ".join(parts) if parts else "no activity"

    # ----- Build markdown body -----
    from core.timefmt import fmt_local, fmt_local_short
    md = []
    md.append(f"# Summary — {fmt_local(now)} (last {window_hours:g}h)")
    md.append("")

    if cash:
        md.append("## Account")
        md.append(f"- **Mode**: `{cash['mode']}`")
        md.append(f"- **Balance**: ${float(cash['current_balance']):,.2f} (start ${float(cash['starting_capital']):,.2f})")
        md.append(f"- **Daily P&L**: ${float(cash['daily_pnl']):+,.2f} (halt at -${float(cash['daily_loss_halt']):,.0f})")
        md.append("")

    md.append(f"## Positions ({metrics.get('open_position_count', 0)} open)")
    if open_positions:
        md.append("| ID | Strategy | Structure | Qty | Open | Mark | Unreal P&L |")
        md.append("|---|---|---|---|---|---|---|")
        for p in open_positions[:20]:
            mark = p.get("latest_mark")
            upnl = p.get("latest_upnl")
            md.append(
                f"| {p['id']} | {p.get('strategy_id') or '-'} | {p['structure']} | "
                f"{p['qty']} | {float(p['open_debit']):.2f} | "
                f"{f'{float(mark):.2f}' if mark is not None else '-'} | "
                f"{f'${float(upnl):+,.0f}' if upnl is not None else '-'} |"
            )
        if metrics.get("total_unrealized_pnl"):
            md.append(f"\n**Total unrealized P&L**: ${metrics['total_unrealized_pnl']:+,.0f}")
    else:
        md.append("_No open positions_")
    md.append("")

    if new_positions:
        md.append(f"## New positions opened ({len(new_positions)})")
        for p in new_positions:
            md.append(
                f"- **#{p['id']}** {p['structure']} qty={p['qty']} @ "
                f"{float(p['open_debit']):.2f} ({p.get('strategy_id') or '-'})"
            )
        md.append("")

    if closed_positions:
        md.append(f"## Positions closed ({len(closed_positions)})")
        md.append(
            f"Realized P&L this window: **${metrics.get('realized_pnl_window', 0):+,.0f}** "
            f"({metrics.get('winners_in_window', 0)}W / {metrics.get('losers_in_window', 0)}L)"
        )
        for p in closed_positions:
            r = float(p.get("realized_pnl") or 0)
            icon = "✅" if r > 0 else ("❌" if r < 0 else "⚪")
            md.append(
                f"- {icon} **#{p['id']}** {p['structure']} → "
                f"${r:+,.0f} (exit: `{p.get('exit_reason') or '-'}`)"
            )
        md.append("")

    md.append("## Recommendations this window")
    if rec_counts:
        for status in ("executed", "approved", "pending", "rejected", "expired"):
            n = rec_counts.get(status, 0)
            if n:
                md.append(f"- **{status}**: {n}")
    else:
        md.append("_None_")

    if top_rejections:
        md.append("")
        md.append("**Top rejection reasons**")
        for r in top_rejections:
            reason = r.get("rejection_reason") or "(no reason)"
            md.append(f"- `{r['n']}×` {reason}")
    md.append("")

    if fut_moves:
        md.append("## Futures price moves")
        md.append("| Symbol | Start | End | Change | % |")
        md.append("|---|---|---|---|---|")
        for sym, m in fut_moves.items():
            md.append(
                f"| {sym} | {m['start']:.2f} | {m['end']:.2f} | "
                f"{m['change']:+.2f} | {m['pct']:+.2f}% |"
            )
        md.append("")

    if alerts:
        md.append(f"## Alerts ({len(alerts)})")
        for a in alerts[:10]:
            level_icon = {"warn": "⚠️", "alert": "🚨", "critical": "🔴"}.get(a["level"], "•")
            ts_local = fmt_local_short(a["ts"])
            md.append(f"- {level_icon} `{ts_local}` [{a.get('topic') or '-'}] {a['message']}")
        md.append("")

    if cost_map:
        md.append("## Costs this window")
        total_cost = sum(cost_map.values())
        for cat, amt in cost_map.items():
            md.append(f"- **{cat}**: ${amt:,.4f}")
        md.append(f"- **TOTAL**: ${total_cost:,.4f}")
        md.append("")

    md.append("---")
    md.append(f"_Generated at {fmt_local(now)}_")

    return Summary(
        ts=now,
        period_start=period_start,
        period_end=period_end,
        headline=headline,
        body_md="\n".join(md),
        metrics=metrics,
        is_important=is_important,
    )
