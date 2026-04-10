"""Position manager — marks positions and applies stop/target rules.

Pure rules. No LLM. Runs every snapshot interval, reads the latest parquet,
marks each open position, applies stop/target rules from the strategy, and
emits closing recommendations to the trader daemon when an exit fires.

Loop:
  1. Load all open positions from DB
  2. Load latest snapshot from parquet
  3. For each position, compute current spread mid from leg quotes
  4. Write a position_marks row
  5. Apply core.risk.evaluate_exit using the strategy's stop rules
  6. If exit triggered, insert a "closing" recommendation that the trader daemon
     will pick up and turn into a sell order
  7. Update daily P&L if a close happened (Phase 2 — for now we just mark)

Marking math:
  spread_mid = sum(qty_i * sign_i * mid_i)  for each leg
  where sign = +1 for BUY legs (we paid), -1 for SELL legs (we received)
  unrealized_pnl = (current_mark - open_debit) * qty * 1000  (debit basis)
  For credit trades, open_debit is stored as a negative number; same formula works.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from core.db import (
    get_peak_unrealized_pnl,
    insert_recommendation,
    list_open_positions_full,
    transaction,
    update_position_status,
    write_commentary,
    write_position_mark,
)
from core.risk import ExitContext, ExitDecision, StopRules, evaluate_exit
from core.snapshot import latest_snapshot

CL_MULT = 1000

# Cache of strategy params loaded from config/strategies.yaml. Populated lazily.
_STRATEGY_CACHE: dict[str, dict] = {}
_STRATEGY_CACHE_MTIME: float = 0.0


def _load_strategies_yaml() -> dict[str, dict]:
    """Load strategy params from config/strategies.yaml, cached by mtime."""
    global _STRATEGY_CACHE, _STRATEGY_CACHE_MTIME
    import os
    yaml_path = Path("config/strategies.yaml")
    if not yaml_path.exists():
        return {}
    try:
        mtime = os.stat(yaml_path).st_mtime
    except OSError:
        return _STRATEGY_CACHE
    if mtime == _STRATEGY_CACHE_MTIME and _STRATEGY_CACHE:
        return _STRATEGY_CACHE
    try:
        import yaml
        cfg = yaml.safe_load(yaml_path.read_text()) or {}
    except Exception:
        return _STRATEGY_CACHE
    _STRATEGY_CACHE = {
        s["id"]: s.get("params", {}) for s in cfg.get("strategies", []) if s.get("id")
    }
    _STRATEGY_CACHE_MTIME = mtime
    return _STRATEGY_CACHE


def _strategy_stop_rules(strategy_id: str | None, structure: str | None) -> StopRules:
    """Return StopRules for a strategy, loaded from strategies.yaml.

    Falls back to a conservative credit-trade default if the strategy isn't
    found in the yaml. The caller passes the structure so we can auto-detect
    debit vs credit for fallback.
    """
    params: dict = {}
    if strategy_id:
        params = _load_strategies_yaml().get(strategy_id, {})

    # Auto-detect credit vs debit from structure name when not explicit
    is_credit_default = True
    if structure:
        s = structure.lower()
        if "debit" in s or "butterfly" in s or "strangle" in s or "straddle" in s:
            is_credit_default = False

    return StopRules(
        profit_target_pct=float(params.get("profit_target_pct", 0.50)),
        stop_loss_pct=float(params.get("stop_loss_pct", 1.00)),
        time_stop_dte=int(params.get("time_stop_dte", 3)),
        is_credit=bool(params.get("is_credit", is_credit_default)),
        # Enhanced rules (None = disabled)
        vol_crush_exit_pts=params.get("vol_crush_exit_pts"),
        trail_activate_pct=params.get("trail_activate_pct"),
        trail_giveback_pct=params.get("trail_giveback_pct"),
        short_strike_buffer_pct=params.get("short_strike_buffer_pct"),
        min_combo_spread_pct=params.get("min_combo_spread_pct"),
    )


def build_exit_context(
    position: dict,
    snap: pd.DataFrame,
    mark_data: dict,
    db_path: str,
) -> ExitContext:
    """Build an ExitContext from the position row + latest snapshot + marks.

    Any field that can't be computed is left as None, which causes the
    corresponding enhanced exit rule to skip safely.
    """
    ctx = ExitContext()

    # Entry snapshot (stored on position row at open time)
    ctx.entry_atm_iv = position.get("entry_atm_iv")
    ctx.entry_underlying = position.get("entry_underlying")

    # Current ATM IV for this position's expiry
    legs = position.get("legs") or []
    if legs and not snap.empty:
        first = legs[0]
        tc = first.get("trading_class")
        expiry = first.get("expiry")
        if tc and expiry:
            sub = snap[
                (snap["sec_type"] == "FOP")
                & (snap["trading_class"] == tc)
                & (snap["expiry"] == expiry)
                & (snap["iv"] > 0)
            ]
            if not sub.empty:
                try:
                    spot = float(sub["underlying_last"].iloc[0])
                    if spot == spot and spot > 0:
                        ctx.current_underlying = spot
                        closest = sub.iloc[(sub["strike"] - spot).abs().argsort()[:2]]
                        atm_iv = float(closest["iv"].mean())
                        if atm_iv == atm_iv and atm_iv > 0:
                            ctx.current_atm_iv = atm_iv
                except (ValueError, TypeError):
                    pass

    # Short strike — nearest-to-money short leg (credit trades)
    if ctx.current_underlying is not None:
        short_legs = [l for l in legs if l.get("action") == "SELL"]
        if short_legs:
            try:
                ranked = sorted(
                    short_legs,
                    key=lambda l: abs(float(l["strike"]) - ctx.current_underlying),  # type: ignore
                )
                ctx.short_strike = float(ranked[0]["strike"])
            except (KeyError, TypeError, ValueError):
                pass

    # Unrealized PnL — current is in mark_data; peak from position_marks history
    ctx.current_unrealized_pnl = float(mark_data.get("unrealized_pnl", 0.0))
    try:
        peak = get_peak_unrealized_pnl(db_path, int(position["id"]))
        # Include the CURRENT mark too in case it's a new high and hasn't been
        # persisted yet this tick
        if peak is None:
            ctx.peak_unrealized_pnl = ctx.current_unrealized_pnl
        else:
            ctx.peak_unrealized_pnl = max(peak, ctx.current_unrealized_pnl)
    except Exception:
        ctx.peak_unrealized_pnl = ctx.current_unrealized_pnl

    # Combo bid/ask: compute from leg bid/ask if available in snap.
    # For a BUY leg: contribution_to_ask = +ask, contribution_to_bid = +bid
    # For a SELL leg: contribution_to_ask = -bid, contribution_to_bid = -ask
    if legs and not snap.empty:
        combo_bid = 0.0
        combo_ask = 0.0
        ok = True
        for leg in legs:
            try:
                tc = leg.get("trading_class")
                expiry = leg["expiry"]
                strike = float(leg["strike"])
                right = leg["right"]
                ratio = int(leg.get("ratio", 1))
                action = leg["action"]
            except (KeyError, TypeError, ValueError):
                ok = False
                break
            rows = snap[
                (snap["sec_type"] == "FOP")
                & (snap["trading_class"] == tc)
                & (snap["expiry"] == expiry)
                & (snap["strike"] == strike)
                & (snap["right"] == right)
            ]
            if rows.empty:
                ok = False
                break
            r = rows.iloc[0]
            bid = r.get("bid")
            ask = r.get("ask")
            if (
                bid is None or ask is None
                or not (bid == bid) or not (ask == ask)
                or bid <= 0 or ask <= 0
            ):
                ok = False
                break
            if action == "BUY":
                combo_bid += ratio * float(bid)
                combo_ask += ratio * float(ask)
            else:
                combo_bid -= ratio * float(ask)
                combo_ask -= ratio * float(bid)
        if ok:
            # Ensure combo_bid <= combo_ask (can flip sign for credit trades)
            lo = min(combo_bid, combo_ask)
            hi = max(combo_bid, combo_ask)
            ctx.combo_bid = lo
            ctx.combo_ask = hi

    return ctx


def mark_position(position: dict, snap: pd.DataFrame) -> Optional[dict]:
    """Compute current mark for a position from the latest snapshot.

    Returns a dict with mark, unrealized_pnl, greeks, or None if quotes are
    missing for any leg.
    """
    if snap.empty or not position.get("legs"):
        return None

    spread_mid = 0.0
    delta_sum = 0.0
    gamma_sum = 0.0
    vega_sum = 0.0
    theta_sum = 0.0
    underlying_last: Optional[float] = None

    for leg in position["legs"]:
        try:
            tc = leg.get("trading_class", "LO")
            expiry = leg["expiry"]
            strike = float(leg["strike"])
            right = leg["right"]
            ratio = int(leg.get("ratio", 1))
            action = leg["action"]  # 'BUY' or 'SELL'
        except (KeyError, TypeError, ValueError):
            return None

        rows = snap[
            (snap["sec_type"] == "FOP")
            & (snap["trading_class"] == tc)
            & (snap["expiry"] == expiry)
            & (snap["strike"] == strike)
            & (snap["right"] == right)
        ]
        if rows.empty:
            return None

        r = rows.iloc[0]
        # Use mid; fall back to last
        mid = r.get("mid")
        if not (mid == mid) or mid <= 0:
            mid = r.get("last")
            if not (mid == mid) or mid <= 0:
                return None
        mid = float(mid)

        sign = 1.0 if action == "BUY" else -1.0
        spread_mid += sign * ratio * mid

        d = float(r.get("delta") or 0.0)
        g = float(r.get("gamma") or 0.0)
        v = float(r.get("vega") or 0.0)
        t = float(r.get("theta") or 0.0)
        delta_sum += sign * ratio * d
        gamma_sum += sign * ratio * g
        vega_sum += sign * ratio * v
        theta_sum += sign * ratio * t

        if underlying_last is None and r.get("underlying_last") is not None:
            underlying_last = float(r["underlying_last"])

    qty = int(position.get("qty") or 0)
    open_debit = float(position.get("open_debit") or 0.0)
    # P&L basis: (current_mark - open_debit) * qty * mult
    # For debit trades open_debit > 0, mark grows → profit
    # For credit trades open_debit < 0, mark grows → loss (but we sold for negative)
    # We store credit trades as negative open_debit; spread_mid here is the
    # current value of the long-the-spread perspective. To express P&L as
    # "did we make money", multiply by qty.
    unrealized = (spread_mid - open_debit) * qty * CL_MULT

    return {
        "mark": spread_mid,
        "unrealized_pnl": unrealized,
        "delta": delta_sum,
        "gamma": gamma_sum,
        "vega": vega_sum,
        "theta": theta_sum,
        "underlying_last": underlying_last,
    }


def build_closing_recommendation(position: dict, exit_reason: str) -> dict:
    """Build a recommendation row that asks the trader daemon to close a position.

    The trader daemon recognizes recommendations with source='position_manager'
    and structure='close_position' as exits, not new openings.
    """
    # Flip every leg's action to close
    closing_legs = []
    for leg in position.get("legs", []):
        new_leg = dict(leg)
        new_leg["action"] = "SELL" if leg["action"] == "BUY" else "BUY"
        closing_legs.append(new_leg)

    ts_opened = position.get("ts_opened")
    if hasattr(ts_opened, "date"):
        expiry_date = ts_opened.date()
    else:
        expiry_date = date.today()

    return {
        "ts": datetime.now(),
        "source": "position_manager",
        "strategy_id": position.get("strategy_id"),
        "thesis": f"Position {position['id']} exit: {exit_reason}",
        "structure": "close_position",
        "legs": closing_legs,
        "size_units": int(position["qty"]),
        "target_debit": -float(position.get("open_debit") or 0.0),  # we want to undo it
        "max_loss": 0.0,
        "max_profit": 0.0,
        "expected_value": None,
        "expiry_date": expiry_date,
        "confidence": 1.0,
        "status": "pending",
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
class PositionManager:
    def __init__(self, settings: dict, db_path: str, snapshot_dir: str) -> None:
        self.settings = settings
        self.db_path = db_path
        self.snapshot_dir = snapshot_dir
        self._stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._on_signal)
                signal.signal(signal.SIGTERM, self._on_signal)
            except ValueError:
                pass

    def _on_signal(self, signum, frame) -> None:
        logging.info("Received signal %s, shutting down...", signum)
        self._stop.set()

    def tick(self) -> None:
        """Process one cycle: mark all open+closing positions, apply exit rules on open."""
        positions = list_open_positions_full(self.db_path, include_closing=True)
        if not positions:
            return

        snap = latest_snapshot(self.snapshot_dir)
        if snap.empty:
            logging.debug("No snapshot yet; skipping marks")
            return

        today = date.today()
        marks_written = 0
        exits_emitted = 0

        for pos in positions:
            mark_data = mark_position(pos, snap)
            if mark_data is None:
                continue

            try:
                write_position_mark(
                    self.db_path,
                    position_id=pos["id"],
                    mark=mark_data["mark"],
                    unrealized_pnl=mark_data["unrealized_pnl"],
                    delta=mark_data["delta"],
                    gamma=mark_data["gamma"],
                    vega=mark_data["vega"],
                    theta=mark_data["theta"],
                    underlying_last=mark_data["underlying_last"],
                )
                marks_written += 1
            except Exception as e:
                logging.warning("Failed to write mark for position %s: %s", pos["id"], e)
                continue

            # Only evaluate exit rules on 'open' positions; 'closing' positions
            # are still marked above but already have a pending close rec.
            if pos.get("status") != "open":
                continue

            # Apply exit rules
            rules = _strategy_stop_rules(pos.get("strategy_id"), pos.get("structure"))
            # The position manager doesn't know the actual expiry date of the
            # whole spread — use the soonest leg expiry.
            try:
                leg_expiries = [
                    datetime.strptime(leg["expiry"], "%Y%m%d").date()
                    for leg in pos["legs"] if leg.get("expiry")
                ]
                position_expiry = min(leg_expiries) if leg_expiries else (today + timedelta(days=30))
            except (ValueError, TypeError):
                position_expiry = today

            # Build enhanced ExitContext (safe if any field can't be computed)
            context = build_exit_context(pos, snap, mark_data, self.db_path)

            decision = evaluate_exit(
                open_debit=abs(float(pos.get("open_debit") or 0.0)),
                current_mark=abs(mark_data["mark"]),
                expiry_date=position_expiry,
                today=today,
                rules=rules,
                context=context,
            )

            # Wide-spread DEFER is not an exit but it IS something we want to
            # log once per position per tick (not every tick, to avoid spam).
            if not decision.should_exit:
                if decision.reason == "defer":
                    logging.info(
                        "Position %s exit deferred: %s", pos["id"], decision.detail or "-"
                    )
                continue

            if not self._has_pending_close(pos["id"]):
                closing = build_closing_recommendation(pos, decision.reason)
                rec_id = insert_recommendation(self.db_path, closing)
                update_position_status(self.db_path, pos["id"], status="closing")
                detail_str = f" — {decision.detail}" if decision.detail else ""
                write_commentary(
                    self.db_path,
                    (
                        f"Position {pos['id']} exit triggered: {decision.reason}"
                        f"{detail_str} (mark={mark_data['mark']:.4f}, "
                        f"unreal_pnl={mark_data['unrealized_pnl']:.0f})"
                    ),
                    level="alert" if decision.urgency == "urgent" else "info",
                    topic="position",
                    context={
                        "position_id": pos["id"],
                        "rec_id": rec_id,
                        "reason": decision.reason,
                        "detail": decision.detail,
                    },
                )
                exits_emitted += 1

        if marks_written:
            logging.info("position_manager tick: marked %d, exits=%d", marks_written, exits_emitted)

    def _has_pending_close(self, position_id: int) -> bool:
        from core.db import get_conn
        conn = get_conn(self.db_path)
        row = conn.execute(
            """
            SELECT COUNT(*) FROM recommendations
            WHERE source = 'position_manager'
              AND status IN ('pending', 'approved')
              AND thesis LIKE ?
            """,
            [f"Position {position_id} exit:%"],
        ).fetchone()
        return bool(row and row[0] > 0)

    def run(self) -> None:
        interval = float(self.settings["stream"]["snapshot_interval_sec"])
        logging.info("Position manager starting, tick interval=%.1fs", interval)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logging.exception("position_manager tick failed: %s", e)
            self._stop.wait(interval)


def load_settings(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.toml")
    p.add_argument("--db", default=None)
    p.add_argument("--snapshot-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--once", action="store_true", help="run a single tick and exit")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings(Path(args.config))
    db_path = args.db or settings["paths"]["db_path"]
    snapshot_dir = args.snapshot_dir or settings["paths"]["snapshot_dir"]

    pm = PositionManager(settings, db_path, snapshot_dir)
    if args.once:
        pm.tick()
    else:
        pm.run()


if __name__ == "__main__":
    main()
