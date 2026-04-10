"""Trader daemon — single writer for orders and positions.

Loop:
  1. Read pending recommendations from DB
  2. For each:
     - Run capital boundary check (core.sizing.check_proposed_order)
     - Build the combo / order spec
     - In `paper` mode: simulate the fill at the live spread mid (from latest snapshot),
       insert order and position rows, mark recommendation as executed
     - In `draft` mode: insert order in 'draft' status, leave for human approval
     - In `live` mode: TODO Phase 4 — actually call IB
     - In `halt` mode: reject everything
  3. Process closing recommendations from position_manager (source='position_manager')
     by closing the matching position at the current spread mid

This is the ONLY process that may INSERT/UPDATE the orders or positions tables.
Single-writer rule.

Phase 1 scope: paper-mode only. Live execution is Phase 4.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
import time
import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from core.db import (
    count_active_orders_by_strategy,
    get_conn,
    get_current_mode,
    insert_order,
    insert_position,
    list_open_positions_full,
    list_pending_recommendations,
    recommendation_has_active_order,
    transaction,
    update_daily_pnl,
    update_position_status,
    update_recommendation_status,
    write_commentary,
)
from core.dte_policy import (
    DTEPolicyConfig,
    MarketState,
    TradeContext,
    min_dte_for_new_position,
)
from core.sizing import check_proposed_order
from core.snapshot import latest_snapshot
from core.strategy_loader import load_enabled_strategies
from strategies.base import Strategy, StrategySignal

CL_MULT = 1000


# ---------------------------------------------------------------------------
# Live combo mid pricing from snapshot
# ---------------------------------------------------------------------------
def compute_combo_mid_from_snapshot(legs: list[dict], snap: pd.DataFrame) -> Optional[float]:
    """Compute the net debit/credit of a combo from latest leg quotes.

    For each leg:
      sign = +1 if BUY (we pay), -1 if SELL (we receive)
      contribution = sign * ratio * leg_mid

    Returns positive number for net debit, negative for net credit, or None if
    any leg's quote is missing.
    """
    if snap.empty:
        return None
    total = 0.0
    for leg in legs:
        try:
            tc = leg.get("trading_class", "LO")
            expiry = leg["expiry"]
            strike = float(leg["strike"])
            right = leg["right"]
            ratio = int(leg.get("ratio", 1))
            action = leg["action"]
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
        mid = r.get("mid")
        if not (mid == mid) or mid <= 0:
            mid = r.get("last")
            if not (mid == mid) or mid <= 0:
                return None
        sign = 1.0 if action == "BUY" else -1.0
        total += sign * ratio * float(mid)
    return total


# ---------------------------------------------------------------------------
# DTE policy helpers
# ---------------------------------------------------------------------------
def _build_market_state_from_snap(
    snap: pd.DataFrame,
    proposed_dte: int,
    rec_legs: list[dict] | None = None,
) -> MarketState:
    """Pull market state from the snapshot for DTE policy.

    Populates atm_iv from the option chain and combo_bid_ask_pct from the
    proposed trade's legs (if provided).
    """
    state = MarketState(proposed_dte=proposed_dte)
    if snap.empty:
        return state
    fops = snap[(snap["sec_type"] == "FOP") & (snap["iv"] > 0)]
    if not fops.empty:
        try:
            state.atm_iv = float(fops["iv"].median())
        except Exception:
            pass

    # Compute combo bid-ask spread % for the proposed trade
    if rec_legs:
        combo_mid = compute_combo_mid_from_snapshot(rec_legs, snap)
        if combo_mid is not None and abs(combo_mid) > 0.001:
            # Compute combo bid and ask to get the spread
            combo_bid = 0.0
            combo_ask = 0.0
            ok = True
            for leg in rec_legs:
                try:
                    tc = leg.get("trading_class", "LO")
                    rows = snap[
                        (snap["sec_type"] == "FOP")
                        & (snap["trading_class"] == tc)
                        & (snap["expiry"] == leg["expiry"])
                        & (snap["strike"] == float(leg["strike"]))
                        & (snap["right"] == leg["right"])
                    ]
                    if rows.empty:
                        ok = False
                        break
                    r = rows.iloc[0]
                    bid, ask = float(r.get("bid", 0)), float(r.get("ask", 0))
                    if bid <= 0 or ask <= 0:
                        ok = False
                        break
                    ratio = int(leg.get("ratio", 1))
                    if leg["action"] == "BUY":
                        combo_bid += ratio * bid
                        combo_ask += ratio * ask
                    else:
                        combo_bid -= ratio * ask
                        combo_ask -= ratio * bid
                except (KeyError, TypeError, ValueError):
                    ok = False
                    break
            if ok:
                spread = abs(combo_ask - combo_bid)
                mid_abs = abs(combo_mid)
                if mid_abs > 0:
                    state.combo_bid_ask_pct = spread / mid_abs

    return state


def _build_trade_context(rec: dict) -> TradeContext:
    structure = rec.get("structure", "")
    is_credit = float(rec.get("target_debit") or 0.0) < 0
    is_pin = structure in ("butterfly", "long_butterfly", "iron_butterfly")
    return TradeContext(
        structure=structure,
        is_credit=is_credit,
        is_hedge=False,
        is_pin_trade=is_pin,
        has_strong_oi_magnet=False,
        is_scheduled_event_play=False,
        proposed_size_pct_of_book=0.0,
    )


def _dte_for(rec: dict) -> int:
    exp = rec.get("expiry_date")
    exp_date: Optional[date] = None
    if isinstance(exp, str):
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            return 0
    elif isinstance(exp, date):
        exp_date = exp
    elif hasattr(exp, "date"):
        try:
            exp_date = exp.date()
        except Exception:
            return 0
    if exp_date is None:
        return 0
    return (exp_date - date.today()).days


# ---------------------------------------------------------------------------
# Recommendation routing
# ---------------------------------------------------------------------------
def _load_dte_policy_config(settings: dict) -> DTEPolicyConfig:
    """Build a DTEPolicyConfig from the [dte_policy] section of settings."""
    sec = settings.get("dte_policy", {})
    if not sec:
        return DTEPolicyConfig()
    kwargs = {}
    # Map every field in DTEPolicyConfig from the toml section
    import dataclasses
    for f in dataclasses.fields(DTEPolicyConfig):
        if f.name in sec:
            kwargs[f.name] = f.type(sec[f.name]) if f.type in (int, float, bool) else sec[f.name]
    return DTEPolicyConfig(**kwargs)


class TraderDaemon:
    def __init__(
        self,
        settings: dict,
        db_path: str,
        snapshot_dir: str,
        strategies_path: str | None = None,
    ) -> None:
        self.settings = settings
        self.db_path = db_path
        self.snapshot_dir = snapshot_dir
        self.strategies_path = strategies_path or "config/strategies.yaml"
        self.strategies: list[Strategy] = []
        self._loaded_mode: str | None = None
        self._dte_config = _load_dte_policy_config(settings)
        self._last_strategy_eval = 0.0
        self._stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._on_signal)
                signal.signal(signal.SIGTERM, self._on_signal)
            except ValueError:
                pass

    def load_strategies(self, mode: str | None = None) -> None:
        """Load strategies whose tier is executable in the current mode."""
        try:
            mode = mode or get_current_mode(self.db_path) or "paper"
            self.strategies = load_enabled_strategies(self.strategies_path, mode=mode)
            self._loaded_mode = mode
            logging.info("Loaded %d strategies (mode=%s)", len(self.strategies), mode)
        except Exception as e:
            logging.exception("Failed to load strategies: %s", e)
            self.strategies = []
            self._loaded_mode = None

    def _on_signal(self, signum, frame) -> None:
        logging.info("Received signal %s, shutting down...", signum)
        self._stop.set()

    # -----------------------------------------------------------------------
    def _ensure_strategies_for_mode(self, mode: str) -> None:
        """Reload strategies when the executable mode changes."""
        if self._loaded_mode != mode:
            self.load_strategies(mode=mode)

    def evaluate_strategies(self) -> int:
        """Run every loaded strategy and emit recommendations.

        Returns the number of new recommendations inserted.
        """
        if not self.strategies:
            return 0
        snap = latest_snapshot(self.snapshot_dir)
        if snap.empty:
            return 0
        positions = list_open_positions_full(self.db_path, include_closing=True)
        staged_orders_by_strategy = count_active_orders_by_strategy(self.db_path)
        market_state = {
            "snapshot": snap,
            "time": datetime.now(),
            "event_imminent": False,  # Phase 2: news collector sets this
        }
        emitted = 0
        for strat in self.strategies:
            # Filter to positions belonging to this strategy
            strat_positions = [p for p in positions if p.get("strategy_id") == strat.id]
            staged_count = staged_orders_by_strategy.get(strat.id, 0)
            if staged_count:
                strat_positions = strat_positions + [
                    {
                        "id": f"staged:{strat.id}:{i}",
                        "strategy_id": strat.id,
                        "status": "draft",
                    }
                    for i in range(staged_count)
                ]
            try:
                signals = strat.evaluate(market_state, strat_positions)
            except Exception as e:
                logging.exception("Strategy %s evaluate() failed: %s", strat.id, e)
                continue
            current_slots = list(strat_positions)
            for sig in signals:
                if not strat.can_open_more(current_slots):
                    break
                rec_id = self._signal_to_recommendation(strat, sig)
                if rec_id:
                    current_slots.append(
                        {"id": f"pending:{rec_id}", "strategy_id": strat.id, "status": "pending"}
                    )
                    emitted += 1
        if emitted:
            logging.info("Strategy eval emitted %d recommendations", emitted)
        return emitted

    def _signal_to_recommendation(self, strat: Strategy, sig: StrategySignal) -> Optional[int]:
        """Convert a StrategySignal into a recommendations row."""
        from core.db import insert_recommendation
        legs_json = []
        for leg in sig.legs:
            inst = leg.instrument
            entry = {
                "trading_class": getattr(inst, "trading_class", ""),
                "expiry": getattr(inst, "expiry", ""),
                "strike": float(getattr(inst, "strike", 0) or 0),
                "right": getattr(inst, "right", ""),
                "ratio": leg.ratio,
                "action": leg.action,
                "symbol": getattr(inst, "symbol", "CL"),
            }
            legs_json.append(entry)

        try:
            return insert_recommendation(self.db_path, {
                "ts": datetime.now(),
                "source": f"strategy:{strat.id}",
                "strategy_id": strat.id,
                "thesis": sig.thesis,
                "structure": sig.structure,
                "legs": legs_json,
                "size_units": int(sig.qty),
                "target_debit": float(sig.target_debit),
                "max_loss": float(sig.max_loss),
                "max_profit": float(sig.max_profit),
                "expected_value": float(sig.expected_value) if sig.expected_value is not None else None,
                "expiry_date": sig.expiry_date,
                "confidence": float(sig.confidence),
                "status": "pending",
            })
        except Exception as e:
            logging.exception("Failed to insert recommendation for %s: %s", strat.id, e)
            return None

    # -----------------------------------------------------------------------
    def tick(self) -> None:
        mode = get_current_mode(self.db_path)
        if mode == "halt":
            return
        self._ensure_strategies_for_mode(mode)

        # Strategy evaluation: run on every tick (the strategy itself enforces
        # max_concurrent + DTE filters, so this is cheap and idempotent)
        try:
            self.evaluate_strategies()
        except Exception as e:
            logging.exception("evaluate_strategies failed: %s", e)

        recs = list_pending_recommendations(self.db_path)
        if not recs:
            return

        snap = latest_snapshot(self.snapshot_dir)
        for rec in recs:
            try:
                if rec.get("source") == "position_manager":
                    self._handle_closing_rec(rec, snap, mode)
                else:
                    self._handle_opening_rec(rec, snap, mode)
            except Exception as e:
                logging.exception("Failed to process recommendation %s: %s", rec.get("id"), e)
                update_recommendation_status(
                    self.db_path, rec["id"], "rejected",
                    rejection_reason=f"trader_daemon error: {e}",
                )

    # -----------------------------------------------------------------------
    def _handle_opening_rec(self, rec: dict, snap: pd.DataFrame, mode: str) -> None:
        rec_id = rec["id"]

        if recommendation_has_active_order(self.db_path, rec_id):
            logging.debug("Recommendation #%d already has an active staged/submitted order", rec_id)
            return

        # 1. Capital boundary check
        max_loss = float(rec.get("max_loss") or 0.0)
        size_check = check_proposed_order(
            self.db_path,
            proposed_max_loss=max_loss,
            strategy_id=rec.get("strategy_id"),
        )
        if not size_check.allowed:
            update_recommendation_status(
                self.db_path, rec_id, "rejected",
                approved_by="auto", rejection_reason=size_check.reason,
            )
            write_commentary(
                self.db_path,
                f"Rec #{rec_id} rejected: {size_check.reason}",
                level="warn", topic="trade",
            )
            return

        # 2. DTE policy check
        ctx = _build_trade_context(rec)
        proposed_dte = _dte_for(rec)
        legs = rec["legs"]
        state = _build_market_state_from_snap(snap, proposed_dte, rec_legs=legs)
        dte_decision = min_dte_for_new_position(ctx, state, self._dte_config)
        if dte_decision.blocking or proposed_dte < dte_decision.min_dte:
            reason = (
                f"DTE policy: {dte_decision.scenario.value} requires "
                f"{dte_decision.min_dte} DTE; proposed {proposed_dte}. "
                f"{dte_decision.reason}"
            )
            update_recommendation_status(
                self.db_path, rec_id, "rejected",
                approved_by="auto", rejection_reason=reason,
            )
            write_commentary(
                self.db_path, f"Rec #{rec_id} rejected: {reason}",
                level="warn", topic="trade",
            )
            return

        # 3. Compute the live combo mid (the price we'd actually fill at)
        mid = compute_combo_mid_from_snapshot(legs, snap)
        if mid is None:
            update_recommendation_status(
                self.db_path, rec_id, "rejected",
                approved_by="auto", rejection_reason="missing leg quotes for live mid",
            )
            return

        qty = int(rec.get("size_units") or 1)
        target_debit = float(rec.get("target_debit") or 0.0)

        # 4. Mode handling
        if mode == "draft":
            # Stage the order, leave for human
            order_id = insert_order(self.db_path, {
                "recommendation_id": rec_id,
                "ts_created": datetime.now(),
                "combo_legs": legs,
                "action": "BUY" if target_debit > 0 else "SELL",
                "qty": qty,
                "limit_price": mid,
                "status": "draft",
                "mode": "draft",
                "notes": "awaiting human approval",
            })
            write_commentary(
                self.db_path,
                f"Rec #{rec_id} → draft order #{order_id} @ {mid:.4f} (awaiting human approval)",
                level="info", topic="trade",
            )
            return

        if mode == "paper":
            # Simulate fill immediately at mid
            self._paper_fill(rec, legs, mid, qty, target_debit)
            update_recommendation_status(
                self.db_path, rec_id, "executed",
                approved_by="auto",
            )
            return

        if mode == "live":
            # Phase 4 — not yet implemented
            update_recommendation_status(
                self.db_path, rec_id, "rejected",
                approved_by="auto",
                rejection_reason="live mode not yet implemented",
            )
            return

    def _compute_entry_context(self, legs: list[dict]) -> tuple[Optional[float], Optional[float]]:
        """Pull ATM IV and underlying price at position open time from the
        latest snapshot. Returns (atm_iv, underlying) with None for either
        field that can't be determined.
        """
        try:
            snap = latest_snapshot(self.snapshot_dir)
        except Exception:
            return None, None
        if snap.empty or not legs:
            return None, None
        first = legs[0]
        tc = first.get("trading_class")
        expiry = first.get("expiry")
        if not tc or not expiry:
            return None, None
        sub = snap[
            (snap["sec_type"] == "FOP")
            & (snap["trading_class"] == tc)
            & (snap["expiry"] == expiry)
            & (snap["iv"] > 0)
        ]
        if sub.empty:
            return None, None
        try:
            spot = float(sub["underlying_last"].iloc[0])
        except (ValueError, TypeError):
            return None, None
        if not (spot == spot) or spot <= 0:
            return None, None
        # ATM IV = mean of the two strikes closest to spot
        try:
            closest = sub.iloc[(sub["strike"] - spot).abs().argsort()[:2]]
            atm_iv = float(closest["iv"].mean())
        except Exception:
            return None, spot
        if not (atm_iv == atm_iv) or atm_iv <= 0:
            return None, spot
        return atm_iv, spot

    def _paper_fill(self, rec: dict, legs: list[dict], mid: float, qty: int, target_debit: float) -> None:
        """Insert a paper-mode order + matching position row at the current mid."""
        ts = datetime.now()
        action = "BUY" if target_debit > 0 else "SELL"

        order_id = insert_order(self.db_path, {
            "recommendation_id": rec["id"],
            "ts_created": ts,
            "ts_submitted": ts,
            "ts_filled": ts,
            "combo_legs": legs,
            "action": action,
            "qty": qty,
            "limit_price": mid,
            "status": "filled",
            "fill_price": mid,
            "commission": 0.0,
            "mode": "paper",
            "notes": "paper fill at snapshot mid",
        })

        # Capture entry context for enhanced exit rules (vol-crush, short-strike,
        # trailing stop). These are nullable — if the snapshot doesn't have IV
        # data yet, the enhanced rules will simply be skipped for this position.
        entry_atm_iv, entry_underlying = self._compute_entry_context(legs)

        position_id = insert_position(self.db_path, {
            "recommendation_id": rec["id"],
            "strategy_id": rec.get("strategy_id"),
            "ts_opened": ts,
            "structure": rec.get("structure", "unknown"),
            "legs": legs,
            "qty": qty,
            "open_debit": mid,                 # store the actual fill, not the target
            "status": "open",
            "stop_loss_price": None,
            "profit_target_price": None,
            "mode": "paper",
            "entry_atm_iv": entry_atm_iv,
            "entry_underlying": entry_underlying,
        })

        write_commentary(
            self.db_path,
            f"Paper fill: rec #{rec['id']} → order #{order_id} → position #{position_id} "
            f"({rec.get('structure')} qty={qty} @ {mid:.4f})",
            level="info", topic="trade",
            context={"order_id": order_id, "position_id": position_id, "fill": mid},
        )

    # -----------------------------------------------------------------------
    def _handle_closing_rec(self, rec: dict, snap: pd.DataFrame, mode: str) -> None:
        """Process a position_manager exit recommendation."""
        rec_id = rec["id"]

        # Extract position id from the thesis: "Position {id} exit: ..."
        m = re.match(r"Position (\d+) exit:", rec.get("thesis") or "")
        if not m:
            update_recommendation_status(
                self.db_path, rec_id, "rejected",
                approved_by="auto",
                rejection_reason="malformed closing thesis",
            )
            return
        position_id = int(m.group(1))

        # Find the position — include both 'open' and 'closing' states
        conn = get_conn(self.db_path)
        row = conn.execute(
            """
            SELECT id, structure, qty, open_debit, status, legs, mode, strategy_id
            FROM positions WHERE id = ? AND status IN ('open', 'closing')
            """,
            [position_id],
        ).fetchone()
        if row is None:
            # Already closed; mark rec as executed
            update_recommendation_status(
                self.db_path, rec_id, "executed",
                approved_by="auto",
            )
            return
        pos = {k: row[k] for k in row.keys()}
        try:
            pos["legs"] = json.loads(pos["legs"]) if isinstance(pos["legs"], str) else pos["legs"]
        except (TypeError, json.JSONDecodeError):
            pos["legs"] = []

        # Closing legs are the inversion; compute current mid of the ORIGINAL legs
        # (so the close price is comparable to open_debit)
        original_legs = pos["legs"]
        close_mid = compute_combo_mid_from_snapshot(original_legs, snap)
        if close_mid is None:
            logging.debug(
                "Close rec #%d: missing leg quotes for position #%d; will retry next tick",
                rec_id, position_id,
            )
            return  # leave pending; will retry next tick

        qty = int(pos["qty"])
        open_debit = float(pos["open_debit"])
        # P&L = (close - open) * qty * mult, regardless of debit/credit sign convention
        realized = (close_mid - open_debit) * qty * CL_MULT

        # Extract exit_reason from thesis
        thesis = rec.get("thesis") or ""
        reason_match = re.search(r"exit:\s*(\w+)", thesis)
        exit_reason = reason_match.group(1) if reason_match else "unknown"

        if mode == "paper":
            ts = datetime.now()
            order_id = insert_order(self.db_path, {
                "recommendation_id": rec_id,
                "ts_created": ts,
                "ts_submitted": ts,
                "ts_filled": ts,
                "combo_legs": original_legs,
                "action": "SELL" if open_debit > 0 else "BUY",  # close opposite of open
                "qty": qty,
                "limit_price": close_mid,
                "status": "filled",
                "fill_price": close_mid,
                "commission": 0.0,
                "mode": "paper",
                "notes": f"paper close: {exit_reason}",
            })
            update_position_status(
                self.db_path, position_id,
                status="closed",
                close_credit=close_mid,
                realized_pnl=realized,
                exit_reason=exit_reason,
            )
            update_recommendation_status(
                self.db_path, rec_id, "executed",
                approved_by="auto",
            )
            update_daily_pnl(self.db_path, realized)
            write_commentary(
                self.db_path,
                f"Closed position #{position_id} ({exit_reason}): "
                f"open={open_debit:.4f} close={close_mid:.4f} pnl=${realized:,.0f}",
                level="alert" if realized < 0 else "info",
                topic="position",
                context={"position_id": position_id, "realized_pnl": realized},
            )
            return

        if mode in ("draft", "live"):
            # Phase 2/4: send actual order — not yet implemented.
            # Restore position to 'open' so it continues being marked and
            # the position_manager can re-emit the close rec next tick.
            update_recommendation_status(
                self.db_path, rec_id, "rejected",
                approved_by="auto",
                rejection_reason=f"{mode} mode close not yet implemented",
            )
            update_position_status(self.db_path, position_id, status="open")
            logging.warning(
                "Close rec #%d rejected (%s mode not implemented); "
                "position #%d restored to open", rec_id, mode, position_id,
            )

    # -----------------------------------------------------------------------
    def run(self) -> None:
        self.load_strategies()
        interval = float(self.settings["stream"]["snapshot_interval_sec"])
        logging.info("Trader daemon starting, tick interval=%.1fs", interval)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logging.exception("trader_daemon tick failed: %s", e)
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
    p.add_argument("--once", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings(Path(args.config))
    db_path = args.db or settings["paths"]["db_path"]
    snapshot_dir = args.snapshot_dir or settings["paths"]["snapshot_dir"]

    td = TraderDaemon(settings, db_path, snapshot_dir)
    if args.once:
        td.tick()
    else:
        td.run()


if __name__ == "__main__":
    main()
