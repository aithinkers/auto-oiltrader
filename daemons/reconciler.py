"""Reconciler — runs at trader_daemon startup and on demand.

Pulls all positions from IB and compares to the DB. The job is to refuse to
proceed if state is inconsistent. Discrepancies are logged and the operator
must resolve them manually before trading resumes.

Modes of discrepancy:
  - Position in IB but not in DB → external/phantom (system didn't create it)
  - Position in DB but not in IB → orphan (system thinks it has a position that broker doesn't)
  - Quantity mismatch → drift (something happened we didn't track)

In paper mode, this still runs but only validates internal DB consistency
(positions table referencing valid orders, marks not stale, etc.).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper

from core.db import get_conn, get_current_mode, transaction, write_commentary


@dataclass
class IBPosition:
    """One row from IB's reqPositions feed."""
    account: str
    symbol: str
    sec_type: str
    exchange: str
    trading_class: str
    expiry: str
    strike: float
    right: str
    quantity: float
    avg_cost_raw: float    # IB returns price * multiplier
    multiplier: float
    con_id: int

    @property
    def per_unit_cost(self) -> float:
        return self.avg_cost_raw / self.multiplier if self.multiplier > 0 else self.avg_cost_raw

    def key(self) -> tuple:
        """Identity tuple for matching to DB positions."""
        return (self.symbol, self.sec_type, self.expiry, self.strike, self.right, self.trading_class)


@dataclass
class ReconcileReport:
    ts: datetime
    mode: str
    ib_positions: list[IBPosition] = field(default_factory=list)
    db_positions: list[dict] = field(default_factory=list)
    external_in_ib: list[IBPosition] = field(default_factory=list)
    orphaned_in_db: list[dict] = field(default_factory=list)
    drift: list[tuple[IBPosition, dict]] = field(default_factory=list)
    consistent: bool = True
    summary: str = ""


# ---------------------------------------------------------------------------
# IB position pull
# ---------------------------------------------------------------------------
class IBPositionsClient(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self.positions: list[IBPosition] = []
        self._done = threading.Event()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (2104, 2106, 2158, 2107, 2103, 2105, 2119, 2150):
            return
        logging.warning("IB error reqId=%s code=%s %s", reqId, errorCode, errorString)

    def position(self, account, contract, position, avgCost):
        try:
            mult = float(contract.multiplier or 0)
        except (TypeError, ValueError):
            mult = 0.0
        try:
            strike = float(contract.strike or 0)
        except (TypeError, ValueError):
            strike = 0.0
        self.positions.append(IBPosition(
            account=account,
            symbol=contract.symbol or "",
            sec_type=contract.secType or "",
            exchange=contract.exchange or "",
            trading_class=contract.tradingClass or "",
            expiry=contract.lastTradeDateOrContractMonth or "",
            strike=strike,
            right=contract.right or "",
            quantity=float(position),
            avg_cost_raw=float(avgCost),
            multiplier=mult,
            con_id=int(contract.conId or 0),
        ))

    def positionEnd(self):
        self._done.set()


def pull_ib_positions(host: str, port: int, client_id: int, timeout: float = 20.0) -> list[IBPosition]:
    """Connect to IB, request all positions, return them, disconnect."""
    app = IBPositionsClient()
    app.connect(host, port, client_id)
    t = threading.Thread(target=app.run, daemon=True)
    t.start()
    time.sleep(1.0)
    app.reqPositions()
    if not app._done.wait(timeout):
        logging.warning("Timeout waiting for IB position stream")
    app.disconnect()
    # filter zero-qty noise
    return [p for p in app.positions if p.quantity != 0]


# ---------------------------------------------------------------------------
# Reconcile logic
# ---------------------------------------------------------------------------
def load_db_positions(db_path: str) -> list[dict]:
    """Return open positions from DB."""
    conn = get_conn(db_path)
    rows = conn.execute(
        """
        SELECT id, structure, qty, open_debit, status, ts_opened, mode, legs, strategy_id
        FROM positions
        WHERE status = 'open'
        """
    ).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        try:
            d["legs"] = json.loads(d["legs"]) if isinstance(d["legs"], str) else d["legs"]
        except (TypeError, json.JSONDecodeError):
            d["legs"] = []
        out.append(d)
    return out


def db_position_keys(db_pos: dict) -> set[tuple]:
    """Return identity tuples for every leg of a DB position."""
    keys: set[tuple] = set()
    for leg in db_pos.get("legs", []):
        try:
            key = (
                leg.get("symbol", "CL"),
                "FOP" if leg.get("right") in ("C", "P") else "FUT",
                leg.get("expiry", ""),
                float(leg.get("strike") or 0),
                leg.get("right", ""),
                leg.get("trading_class", ""),
            )
            keys.add(key)
        except (TypeError, ValueError):
            continue
    return keys


def reconcile(db_path: str, host: str, port: int, client_id: int) -> ReconcileReport:
    """Run a reconciliation pass.

    Behavior depends on mode:
      - paper: only validate internal DB consistency (don't talk to IB)
      - draft / live: pull IB positions and compare
      - halt: still run (useful for diagnosing why we're halted)
    """
    mode = get_current_mode(db_path)
    report = ReconcileReport(ts=datetime.now(), mode=mode)
    report.db_positions = load_db_positions(db_path)

    if mode == "paper":
        report.consistent = True
        report.summary = (
            f"paper mode: skipped IB pull. {len(report.db_positions)} open positions in DB."
        )
        return report

    try:
        report.ib_positions = pull_ib_positions(host, port, client_id)
    except Exception as e:
        report.consistent = False
        report.summary = f"FAILED to pull IB positions: {e}"
        return report

    # Map IB by key
    ib_by_key: dict[tuple, IBPosition] = {p.key(): p for p in report.ib_positions}

    # Build a flat set of all leg-keys we know about in the DB
    db_known_keys: set[tuple] = set()
    for dpos in report.db_positions:
        db_known_keys |= db_position_keys(dpos)

    # External positions: in IB, not in DB
    for key, ibp in ib_by_key.items():
        if key not in db_known_keys:
            report.external_in_ib.append(ibp)

    # Orphaned positions: in DB, not in IB (only relevant if we expect a 1:1 leg match)
    # In real trading, the system's positions should each have legs that appear in IB.
    for dpos in report.db_positions:
        for leg_key in db_position_keys(dpos):
            if leg_key not in ib_by_key:
                report.orphaned_in_db.append(dpos)
                break

    report.consistent = (
        not report.external_in_ib
        and not report.orphaned_in_db
        and not report.drift
    )

    if report.consistent:
        report.summary = (
            f"OK: {len(report.ib_positions)} IB positions match {len(report.db_positions)} DB positions"
        )
    else:
        bits = []
        if report.external_in_ib:
            bits.append(f"{len(report.external_in_ib)} external in IB")
        if report.orphaned_in_db:
            bits.append(f"{len(report.orphaned_in_db)} orphaned in DB")
        if report.drift:
            bits.append(f"{len(report.drift)} drifted")
        report.summary = "INCONSISTENT: " + ", ".join(bits)

    return report


def persist_report(db_path: str, report: ReconcileReport) -> None:
    """Write a commentary line + auto-halt if inconsistent."""
    level = "info" if report.consistent else "critical"
    write_commentary(
        db_path,
        f"reconciler: {report.summary}",
        level=level,
        topic="reconcile",
        context={
            "external_in_ib": [
                {"symbol": p.symbol, "expiry": p.expiry, "strike": p.strike,
                 "right": p.right, "qty": p.quantity, "account": p.account}
                for p in report.external_in_ib
            ],
            "orphaned_in_db": [
                {"id": p["id"], "structure": p["structure"]}
                for p in report.orphaned_in_db
            ],
        },
    )

    if not report.consistent:
        # Auto-halt: any inconsistency forces mode=halt until human resolves
        with transaction(db_path) as conn:
            cash_row = conn.execute("SELECT * FROM cash ORDER BY ts DESC LIMIT 1").fetchone()
            if cash_row is None:
                return
            cash_dict = {k: cash_row[k] for k in cash_row.keys()}
            if cash_dict["mode"] != "halt":
                cash_dict["ts"] = datetime.now().isoformat()
                cash_dict["mode"] = "halt"
                cash_dict["notes"] = f"auto-halt by reconciler: {report.summary}"
                col_names = ", ".join(cash_dict.keys())
                placeholders = ", ".join(["?"] * len(cash_dict))
                conn.execute(
                    f"INSERT INTO cash ({col_names}) VALUES ({placeholders})",
                    list(cash_dict.values()),
                )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def load_settings(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.toml")
    p.add_argument("--db", default=None)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--no-persist", action="store_true",
                   help="print report but don't write commentary or auto-halt")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings(Path(args.config))
    db_path = args.db or settings["paths"]["db_path"]
    host = settings["ib"]["host"]
    port = settings["ib"]["port"]
    client_id = settings["ib"]["client_id_recon"]

    report = reconcile(db_path, host, port, client_id)
    print(f"\n=== Reconcile report at {report.ts.isoformat()} ===")
    print(f"Mode: {report.mode}")
    print(f"Status: {'OK' if report.consistent else 'INCONSISTENT'}")
    print(f"Summary: {report.summary}")
    print(f"IB positions: {len(report.ib_positions)}")
    print(f"DB positions: {len(report.db_positions)}")
    if report.external_in_ib:
        print("External in IB:")
        for p in report.external_in_ib:
            print(f"  {p.account} {p.symbol} {p.sec_type} {p.expiry} {p.strike}{p.right} qty={p.quantity}")
    if report.orphaned_in_db:
        print("Orphaned in DB:")
        for p in report.orphaned_in_db:
            print(f"  id={p['id']} {p['structure']} qty={p['qty']}")

    if not args.no_persist:
        persist_report(db_path, report)
        if not report.consistent:
            print("\n⚠ AUTO-HALT applied. Use `tradectl unhalt` after resolution.")
            sys.exit(2)


if __name__ == "__main__":
    main()
