"""FastAPI server — REST endpoints for remote control.

Run:
  uvicorn interfaces.api:app --host 0.0.0.0 --port 8080

Authentication is still intentionally lightweight and aimed at trusted private
network use. Approval endpoints now map to the same shared DB helpers that the
CLI and dashboard use so draft-mode behavior stays consistent across surfaces.
"""

from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from core.db import (
    approve_draft_recommendation,
    get_conn,
    get_current_cash,
    get_recommendation,
    reject_draft_recommendation,
    to_utc_iso,
    transaction,
    utc_now_iso,
)


app = FastAPI(title="Autonomous Oil Trader", version="0.1.0")


class DecisionBody(BaseModel):
    actor: str | None = None
    reason: str | None = None


class ModeBody(BaseModel):
    mode: str


class ObservationBody(BaseModel):
    text: str
    category: str = "user"
    weight: float = 0.6
    ttl_hours: float = 24.0


def _db_path() -> str:
    return os.environ.get("TRADER_DB_PATH", "./data/trader.db")


def _row_dict(row) -> dict:
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


def _normalize(value):
    if is_dataclass(value):
        return asdict(value)
    return value


def _set_mode(new_mode: str, note: str) -> dict:
    if new_mode not in ("paper", "draft", "live", "halt"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {new_mode}")

    with transaction(_db_path()) as conn:
        cash_row = conn.execute("SELECT * FROM cash ORDER BY ts DESC LIMIT 1").fetchone()
        if cash_row is None:
            raise HTTPException(status_code=404, detail="No cash row in DB")
        cash_dict = {k: cash_row[k] for k in cash_row.keys()}
        cash_dict["mode"] = new_mode
        cash_dict["ts"] = utc_now_iso()
        cash_dict["notes"] = note
        col_names = ", ".join(cash_dict.keys())
        placeholders = ", ".join(["?"] * len(cash_dict))
        conn.execute(
            f"INSERT INTO cash ({col_names}) VALUES ({placeholders})",
            list(cash_dict.values()),
        )
    return get_current_cash(_db_path())


@app.get("/")
def root():
    return {"name": "autonomous-oiltrader", "version": "0.1.0"}


@app.get("/status")
def status():
    try:
        cash = get_current_cash(_db_path())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "cash": cash}


@app.get("/positions")
def positions():
    conn = get_conn(_db_path())
    rows = conn.execute(
        """
        SELECT id, recommendation_id, strategy_id, ts_opened, ts_closed, structure,
               qty, open_debit, close_credit, realized_pnl, status, exit_reason, mode
        FROM positions
        WHERE status IN ('open', 'closing')
        ORDER BY ts_opened DESC
        """
    ).fetchall()
    return {"positions": [_row_dict(row) for row in rows]}


@app.get("/recommendations")
def recommendations(status: str | None = Query(default=None)):
    conn = get_conn(_db_path())
    params: list[str] = []
    sql = """
        SELECT r.*,
               (
                   SELECT o.status
                   FROM orders o
                   WHERE o.recommendation_id = r.id
                   ORDER BY o.id DESC
                   LIMIT 1
               ) AS order_status,
               (
                   SELECT o.id
                   FROM orders o
                   WHERE o.recommendation_id = r.id
                   ORDER BY o.id DESC
                   LIMIT 1
               ) AS order_id
        FROM recommendations r
    """
    if status:
        sql += " WHERE r.status = ?"
        params.append(status)
    sql += " ORDER BY r.ts DESC LIMIT 200"
    rows = conn.execute(sql, params).fetchall()
    return {"recommendations": [_row_dict(row) for row in rows]}


@app.post("/recommendations/{rec_id}/approve")
def approve_recommendation(rec_id: int, body: DecisionBody | None = None):
    actor = (body.actor if body and body.actor else None) or os.environ.get("USER", "api")
    ok, message = approve_draft_recommendation(_db_path(), rec_id, actor)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    rec = get_recommendation(_db_path(), rec_id)
    return {"ok": True, "message": message, "recommendation": rec}


@app.post("/recommendations/{rec_id}/reject")
def reject_recommendation(rec_id: int, body: DecisionBody):
    actor = body.actor or os.environ.get("USER", "api")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A rejection reason is required")
    ok, message = reject_draft_recommendation(_db_path(), rec_id, actor, reason)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    rec = get_recommendation(_db_path(), rec_id)
    return {"ok": True, "message": message, "recommendation": rec}


@app.post("/halt")
def halt():
    return {"ok": True, "cash": _set_mode("halt", "API halt")}


@app.post("/unhalt")
def unhalt():
    return {"ok": True, "cash": _set_mode("paper", "API unhalt")}


@app.post("/mode")
def mode(body: ModeBody):
    return {"ok": True, "cash": _set_mode(body.mode, f"API mode change to {body.mode}")}


@app.post("/observations")
def observations(body: ObservationBody):
    expires_at = to_utc_iso(datetime.now(timezone.utc) + timedelta(hours=body.ttl_hours))
    with transaction(_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO user_observations (ts, text, category, weight, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [utc_now_iso(), body.text, body.category, body.weight, expires_at],
        )
    return {"ok": True}


@app.get("/commentary")
def commentary(since: str | None = Query(default=None)):
    conn = get_conn(_db_path())
    if since:
        rows = conn.execute(
            """
            SELECT ts, level, topic, message, context
            FROM commentary
            WHERE ts > ?
            ORDER BY ts DESC
            LIMIT 200
            """,
            [since],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ts, level, topic, message, context
            FROM commentary
            ORDER BY ts DESC
            LIMIT 200
            """
        ).fetchall()
    return {"commentary": [_row_dict(row) for row in rows]}


@app.get("/findings/latest")
def latest_findings():
    conn = get_conn(_db_path())
    row = conn.execute(
        "SELECT * FROM findings ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No findings found")
    data = _row_dict(row)
    return {"finding": {k: _normalize(v) for k, v in data.items()}}
