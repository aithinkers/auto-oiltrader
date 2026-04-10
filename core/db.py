"""SQLite (WAL mode) connection management and query helpers.

We use SQLite with WAL mode rather than DuckDB so that multiple processes
(daemons + dashboard + CLI) can read while one process writes. SQLite handles
concurrent reads natively in WAL mode and is sufficient for the trading-state
DB at this scale.

For analytics on parquet snapshots, code uses DuckDB directly via
read_parquet() — that path doesn't touch this module.

Connection model:
  - Each thread gets its own sqlite3.Connection (sqlite3 disallows sharing)
  - Connections are cached per (db_path, thread_id)
  - WAL mode is enabled on every new connection
  - Writes are serialized at the SQLite level (BEGIN IMMEDIATE)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


def utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 with millisecond precision and 'Z' suffix.

    All DB writers must use this so timestamps sort lexicographically the same
    way SQLite's `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')` does in seed/migration files.
    """
    now = datetime.now(timezone.utc)
    # Match SQLite's %f (3-digit milliseconds) format precisely
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def to_utc_iso(dt: datetime | str) -> str:
    """Convert a datetime or ISO string to the standard UTC ISO format used
    by utc_now_iso(). Naive datetimes (and naive ISO strings) are treated as
    local time. Strings already in the canonical Z format are returned as-is."""
    if isinstance(dt, str):
        # Already in canonical UTC format? Return unchanged.
        if dt.endswith("Z"):
            return dt
        try:
            parsed = datetime.fromisoformat(dt)
        except ValueError:
            return dt  # not parseable; let SQLite handle it
        dt = parsed
    if dt.tzinfo is None:
        # Assume naive datetimes are in local time and convert to UTC
        dt = dt.astimezone(timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_LOCK = threading.Lock()
_CONN_CACHE: dict[tuple[str, int], sqlite3.Connection] = {}


def _enable_wal(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")  # 5s wait on contention


def get_conn(db_path: str | Path, read_only: bool = False) -> sqlite3.Connection:
    """Return a thread-local SQLite connection.

    `read_only` is accepted for backwards compatibility — SQLite WAL allows
    concurrent reads regardless. We open every connection in read-write mode
    so any thread can write through `transaction()`.
    """
    key = (str(db_path), threading.get_ident())
    with _LOCK:
        if key not in _CONN_CACHE:
            conn = sqlite3.connect(
                str(db_path),
                isolation_level=None,  # autocommit mode; transactions are explicit
                check_same_thread=True,
                timeout=10.0,
            )
            conn.row_factory = sqlite3.Row
            _enable_wal(conn)
            _CONN_CACHE[key] = conn
        return _CONN_CACHE[key]


def close_cached(db_path: str | Path | None = None) -> None:
    """Close cached connection(s). Tests use this for clean teardown."""
    with _LOCK:
        if db_path is None:
            for c in _CONN_CACHE.values():
                try:
                    c.close()
                except Exception:
                    pass
            _CONN_CACHE.clear()
        else:
            target_prefix = str(db_path)
            stale = [k for k in _CONN_CACHE if k[0] == target_prefix]
            for k in stale:
                try:
                    _CONN_CACHE[k].close()
                except Exception:
                    pass
                del _CONN_CACHE[k]


@contextmanager
def transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a thread-local connection inside a BEGIN IMMEDIATE/COMMIT block.

    Rolls back on exception.
    """
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def init_schema(db_path: str | Path, schema_sql: str | Path) -> None:
    """Apply schema.sql to a fresh DB. Safe to re-run."""
    sql = Path(schema_sql).read_text()
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        _enable_wal(conn)
        conn.executescript(sql)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers — every query returns dicts (Row objects act as both)
# ---------------------------------------------------------------------------
def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def get_current_cash(db_path: str | Path) -> dict:
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM cash ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("No cash row in DB. Run db/seed.sql first.")
    return _row_to_dict(row)


def get_current_mode(db_path: str | Path) -> str:
    return get_current_cash(db_path)["mode"]


def list_open_positions(db_path: str | Path) -> list[dict]:
    conn = get_conn(db_path)
    rows = conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY ts_opened").fetchall()
    return [_row_to_dict(r) for r in rows]


def list_open_positions_full(db_path: str | Path, include_closing: bool = False) -> list[dict]:
    """Return open (and optionally closing) positions with parsed legs JSON.

    Selects * so new columns (entry_atm_iv, entry_underlying, etc.) become
    available to callers automatically.

    When include_closing=True, positions in 'closing' state are also returned
    so they continue to be marked and managed until actually closed.
    """
    conn = get_conn(db_path)
    if include_closing:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'closing') ORDER BY ts_opened"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY ts_opened"
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        if d is None:
            continue
        try:
            d["legs"] = json.loads(d["legs"]) if isinstance(d["legs"], str) else d["legs"]
        except (TypeError, json.JSONDecodeError):
            d["legs"] = []
        out.append(d)
    return out


def get_peak_unrealized_pnl(db_path: str | Path, position_id: int) -> Optional[float]:
    """Return the maximum unrealized_pnl observed during the position's life.

    Used by the trailing-stop rule in core.risk.evaluate_exit.
    """
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT MAX(unrealized_pnl) AS peak FROM position_marks WHERE position_id = ?",
        [position_id],
    ).fetchone()
    if row is None or row["peak"] is None:
        return None
    try:
        return float(row["peak"])
    except (TypeError, ValueError):
        return None


def list_pending_recommendations(db_path: str | Path) -> list[dict]:
    conn = get_conn(db_path)
    rows = conn.execute(
        """
        SELECT id, ts, source, strategy_id, thesis, structure, legs, size_units,
               target_debit, max_loss, max_profit, expected_value, expiry_date,
               confidence
        FROM recommendations
        WHERE status = 'pending'
        ORDER BY ts
        """
    ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        try:
            d["legs"] = json.loads(d["legs"]) if isinstance(d["legs"], str) else d["legs"]
        except (TypeError, json.JSONDecodeError):
            d["legs"] = []
        out.append(d)
    return out


def get_recommendation(db_path: str | Path, rec_id: int) -> dict | None:
    """Return one recommendation row as a dict, or None if not found."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM recommendations WHERE id = ?",
        [rec_id],
    ).fetchone()
    return _row_to_dict(row)


def get_latest_order_for_recommendation(
    db_path: str | Path,
    rec_id: int,
    statuses: tuple[str, ...] | None = None,
) -> dict | None:
    """Return the latest order tied to a recommendation, optionally filtered by status."""
    conn = get_conn(db_path)
    if statuses:
        placeholders = ", ".join(["?"] * len(statuses))
        row = conn.execute(
            f"""
            SELECT * FROM orders
            WHERE recommendation_id = ?
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            [rec_id, *statuses],
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM orders
            WHERE recommendation_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            [rec_id],
        ).fetchone()
    return _row_to_dict(row)


def recommendation_has_active_order(db_path: str | Path, rec_id: int) -> bool:
    """Whether the recommendation already has a staged or submitted order."""
    conn = get_conn(db_path)
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM orders
        WHERE recommendation_id = ?
          AND status IN ('draft', 'submitted')
        """,
        [rec_id],
    ).fetchone()
    return bool(row and row["n"] > 0)


def count_active_orders_by_strategy(db_path: str | Path) -> dict[str, int]:
    """Return counts of staged/submitted orders grouped by strategy_id."""
    conn = get_conn(db_path)
    rows = conn.execute(
        """
        SELECT r.strategy_id, COUNT(*) AS n
        FROM orders o
        JOIN recommendations r ON r.id = o.recommendation_id
        WHERE o.status IN ('draft', 'submitted')
          AND r.strategy_id IS NOT NULL
        GROUP BY r.strategy_id
        """
    ).fetchall()
    out: dict[str, int] = {}
    for row in rows:
        try:
            out[str(row["strategy_id"])] = int(row["n"])
        except (TypeError, ValueError):
            continue
    return out


def insert_recommendation(db_path: str | Path, rec: dict) -> int:
    rec_to_insert = dict(rec)
    if "legs" in rec_to_insert and not isinstance(rec_to_insert["legs"], str):
        rec_to_insert["legs"] = json.dumps(rec_to_insert["legs"])
    if "ts" in rec_to_insert and isinstance(rec_to_insert["ts"], (datetime, str)):
        rec_to_insert["ts"] = to_utc_iso(rec_to_insert["ts"])
    if "expiry_date" in rec_to_insert and hasattr(rec_to_insert["expiry_date"], "isoformat"):
        # expiry_date is a date (not datetime); store as plain ISO date string
        rec_to_insert["expiry_date"] = rec_to_insert["expiry_date"].isoformat()
    cols = ", ".join(rec_to_insert.keys())
    placeholders = ", ".join(["?"] * len(rec_to_insert))
    with transaction(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO recommendations ({cols}) VALUES ({placeholders})",
            list(rec_to_insert.values()),
        )
        return int(cur.lastrowid)


def insert_position(db_path: str | Path, pos: dict) -> int:
    rec = dict(pos)
    if "legs" in rec and not isinstance(rec["legs"], str):
        rec["legs"] = json.dumps(rec["legs"])
    for k in ("ts_opened", "ts_closed"):
        if k in rec and rec[k] is not None and isinstance(rec[k], (datetime, str)):
            rec[k] = to_utc_iso(rec[k])
    cols = ", ".join(rec.keys())
    placeholders = ", ".join(["?"] * len(rec))
    with transaction(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({placeholders})",
            list(rec.values()),
        )
        return int(cur.lastrowid)


def insert_order(db_path: str | Path, order: dict) -> int:
    rec = dict(order)
    if "combo_legs" in rec and not isinstance(rec["combo_legs"], str):
        rec["combo_legs"] = json.dumps(rec["combo_legs"])
    for k in ("ts_created", "ts_submitted", "ts_filled"):
        if k in rec and rec[k] is not None and isinstance(rec[k], (datetime, str)):
            rec[k] = to_utc_iso(rec[k])
    cols = ", ".join(rec.keys())
    placeholders = ", ".join(["?"] * len(rec))
    with transaction(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO orders ({cols}) VALUES ({placeholders})",
            list(rec.values()),
        )
        return int(cur.lastrowid)


def update_position_status(
    db_path: str | Path,
    position_id: int,
    status: str,
    close_credit: float | None = None,
    realized_pnl: float | None = None,
    exit_reason: str | None = None,
) -> None:
    with transaction(db_path) as conn:
        if status == "closed":
            conn.execute(
                """
                UPDATE positions
                SET status = ?, close_credit = ?, realized_pnl = ?, exit_reason = ?,
                    ts_closed = ?
                WHERE id = ?
                """,
                [status, close_credit, realized_pnl, exit_reason,
                 utc_now_iso(), position_id],
            )
        else:
            conn.execute(
                """
                UPDATE positions
                SET status = ?, close_credit = ?, realized_pnl = ?, exit_reason = ?
                WHERE id = ?
                """,
                [status, close_credit, realized_pnl, exit_reason, position_id],
            )


def update_recommendation_status(
    db_path: str | Path,
    rec_id: int,
    status: str,
    approved_by: str | None = None,
    rejection_reason: str | None = None,
) -> None:
    approved_at = utc_now_iso() if status in ("approved", "executed") else None
    with transaction(db_path) as conn:
        conn.execute(
            """
            UPDATE recommendations
            SET status = ?, approved_by = ?, approved_at = ?, rejection_reason = ?
            WHERE id = ?
            """,
            [status, approved_by, approved_at, rejection_reason, rec_id],
        )


def update_order_status(
    db_path: str | Path,
    order_id: int,
    status: str,
    notes: str | None = None,
    *,
    append_note: bool = False,
) -> None:
    """Update an order status and optionally replace/append notes.

    `submitted` stamps `ts_submitted` the first time it is set.
    """
    with transaction(db_path) as conn:
        existing = conn.execute(
            "SELECT notes, ts_submitted FROM orders WHERE id = ?",
            [order_id],
        ).fetchone()
        if existing is None:
            raise ValueError(f"Order {order_id} not found")

        next_notes = existing["notes"]
        if notes:
            if append_note and next_notes:
                next_notes = f"{next_notes}\n{notes}"
            else:
                next_notes = notes

        if status == "submitted":
            ts_submitted = existing["ts_submitted"] or utc_now_iso()
            conn.execute(
                """
                UPDATE orders
                SET status = ?, ts_submitted = ?, notes = ?
                WHERE id = ?
                """,
                [status, ts_submitted, next_notes, order_id],
            )
        else:
            conn.execute(
                """
                UPDATE orders
                SET status = ?, notes = ?
                WHERE id = ?
                """,
                [status, next_notes, order_id],
            )


def approve_draft_recommendation(
    db_path: str | Path,
    rec_id: int,
    actor: str,
) -> tuple[bool, str]:
    """Approve a staged draft recommendation.

    Approval is recorded on the recommendation. The order remains in `draft`
    because actual broker submission is not implemented yet.
    """
    rec = get_recommendation(db_path, rec_id)
    if rec is None:
        return False, f"Recommendation {rec_id} not found"
    if rec.get("status") != "pending":
        return False, f"Recommendation {rec_id} is {rec.get('status')}, expected pending"

    order = get_latest_order_for_recommendation(db_path, rec_id, statuses=("draft",))
    if order is None:
        return False, f"Recommendation {rec_id} has no staged draft order"

    update_recommendation_status(db_path, rec_id, "approved", approved_by=actor)
    update_order_status(
        db_path,
        int(order["id"]),
        "draft",
        notes=f"{utc_now_iso()} draft approved by {actor}; broker submission not yet implemented",
        append_note=True,
    )
    write_commentary(
        db_path,
        f"Draft recommendation #{rec_id} approved by {actor}; staged order remains draft until broker-submit flow exists",
        level="info",
        topic="trade",
        context={"recommendation_id": rec_id, "order_id": int(order["id"]), "actor": actor},
    )
    return True, f"Approved recommendation #{rec_id}. Draft order remains staged; broker submission is not implemented yet."


def reject_draft_recommendation(
    db_path: str | Path,
    rec_id: int,
    actor: str,
    reason: str,
) -> tuple[bool, str]:
    """Reject a staged draft recommendation and cancel its draft order if present."""
    rec = get_recommendation(db_path, rec_id)
    if rec is None:
        return False, f"Recommendation {rec_id} not found"
    if rec.get("status") not in ("pending", "approved"):
        return False, f"Recommendation {rec_id} is {rec.get('status')}, expected pending/approved"

    order = get_latest_order_for_recommendation(db_path, rec_id, statuses=("draft", "submitted"))
    if order is not None:
        update_order_status(
            db_path,
            int(order["id"]),
            "cancelled",
            notes=f"{utc_now_iso()} draft rejected by {actor}: {reason}",
            append_note=True,
        )

    update_recommendation_status(db_path, rec_id, "rejected", rejection_reason=reason)
    write_commentary(
        db_path,
        f"Draft recommendation #{rec_id} rejected by {actor}: {reason}",
        level="warn",
        topic="trade",
        context={"recommendation_id": rec_id, "order_id": int(order['id']) if order else None, "actor": actor},
    )
    return True, f"Rejected recommendation #{rec_id}."


def write_position_mark(
    db_path: str | Path,
    position_id: int,
    mark: float,
    unrealized_pnl: float,
    delta: float | None = None,
    gamma: float | None = None,
    vega: float | None = None,
    theta: float | None = None,
    underlying_last: float | None = None,
) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO position_marks
              (position_id, ts, mark, unrealized_pnl, delta, gamma, vega, theta, underlying_last)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [position_id, utc_now_iso(), mark, unrealized_pnl,
             delta, gamma, vega, theta, underlying_last],
        )


def update_daily_pnl(db_path: str | Path, pnl_delta: float) -> None:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM cash ORDER BY ts DESC LIMIT 1").fetchone()
        if row is None:
            return
        cash_dict = _row_to_dict(row)
        cash_dict["ts"] = utc_now_iso()
        cash_dict["daily_pnl"] = float(cash_dict.get("daily_pnl") or 0) + pnl_delta
        cash_dict["current_balance"] = float(cash_dict.get("current_balance") or 0) + pnl_delta
        cash_dict["high_watermark"] = max(
            float(cash_dict.get("high_watermark") or 0), cash_dict["current_balance"]
        )
        cash_dict["notes"] = "pnl update"
        col_names = ", ".join(cash_dict.keys())
        placeholders = ", ".join(["?"] * len(cash_dict))
        conn.execute(
            f"INSERT INTO cash ({col_names}) VALUES ({placeholders})",
            list(cash_dict.values()),
        )


def get_open_position_local_symbols(db_path: str | Path) -> set[str]:
    """Return set of futures local_symbols referenced by open positions.

    Walks the legs JSON of each open position and extracts the underlying
    local_symbol. Used by the rolling window to keep marking aged-out contracts.
    """
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT legs FROM positions WHERE status='open'"
    ).fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            legs = json.loads(r["legs"]) if isinstance(r["legs"], str) else r["legs"]
            for leg in legs or []:
                und = leg.get("underlying_local_symbol") or leg.get("local_symbol")
                if und:
                    out.add(und)
        except Exception:
            continue
    return out


def write_active_contracts(db_path: str | Path, window) -> None:
    """Snapshot the current rolling window to active_contracts."""
    ts = utc_now_iso()
    rows: list[tuple] = []
    for c in window.tradeable:
        rows.append((ts, c.local_symbol, c.symbol, c.expiry, c.con_id, "tradeable", c.dte(), "in window"))
    for c in window.markable:
        rows.append((ts, c.local_symbol, c.symbol, c.expiry, c.con_id, "markable", c.dte(), "open position past drop"))
    if not rows:
        return
    with transaction(db_path) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO active_contracts (ts, local_symbol, symbol, expiry, con_id, state, dte_at_change, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def write_roll_event(db_path: str | Path, added: list[str], removed: list[str], reason: str) -> None:
    if not added and not removed:
        return
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO roll_events (ts, added, removed, reason, notes) VALUES (?, ?, ?, ?, ?)",
            [utc_now_iso(), ",".join(added), ",".join(removed), reason, None],
        )


def write_commentary(
    db_path: str | Path,
    message: str,
    level: str = "info",
    topic: str | None = None,
    context: dict | None = None,
) -> None:
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO commentary (ts, level, topic, message, context) VALUES (?, ?, ?, ?, ?)",
            [utc_now_iso(), level, topic, message,
             json.dumps(context) if context else None],
        )
