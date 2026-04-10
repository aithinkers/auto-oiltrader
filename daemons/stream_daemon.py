"""Stream daemon — IB market data → parquet snapshots.

Connects to IB, discovers CL futures + LO option chains, subscribes to live
market data, snapshots to parquet every snapshot_interval_sec, and rebuilds
the rolling window of active contracts on a daily schedule.

This is the foundation daemon — every other process depends on the snapshot
files this writes.

Run:
    python -m daemons.stream_daemon

Or via systemd / launchd (see services/).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq

from ibapi.client import EClient
from ibapi.contract import Contract, ContractDetails
from ibapi.wrapper import EWrapper

from core.db import (
    get_open_position_local_symbols,
    write_active_contracts,
    write_commentary,
    write_roll_event,
)
from core.rolling_window import (
    ActiveWindow,
    ContractInfo,
    compute_active_window,
    diff_windows,
    needs_rebuild,
)

# ---------------------------------------------------------------------------
# Tick types we care about
# ---------------------------------------------------------------------------
TICK_BID = 1
TICK_ASK = 2
TICK_LAST = 4
TICK_VOLUME = 8
TICK_OPTION_CALL_OI = 27
TICK_OPTION_PUT_OI = 28
TICK_FUT_OI = 86

GENERIC_TICKS_FUTURE = "588,165,375"
GENERIC_TICKS_OPTION = "100,101,106,165,225"


# ---------------------------------------------------------------------------
# In-memory quote state per subscription
# ---------------------------------------------------------------------------
@dataclass
class Quote:
    sec_type: str = ""
    symbol: str = "CL"
    local_symbol: str = ""              # for FUT only
    underlying_local_symbol: str = ""   # for FOP — which future this option is on
    trading_class: str = ""
    expiry: str = ""
    strike: float = 0.0
    right: str = ""
    bid: float = float("nan")
    ask: float = float("nan")
    last: float = float("nan")
    volume: int = 0
    open_interest: int = 0
    iv: float = float("nan")
    delta: float = float("nan")
    gamma: float = float("nan")
    vega: float = float("nan")
    theta: float = float("nan")
    last_update: float = 0.0


# ---------------------------------------------------------------------------
# IB API client
# ---------------------------------------------------------------------------
class IBStream(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._next_req_id = 9000
        self._req_id_lock = threading.Lock()

        # contract discovery state
        self._cd_results: dict[int, list[ContractDetails]] = {}
        self._cd_done: dict[int, threading.Event] = {}

        self._opt_params: list[dict] = []
        self._opt_params_done = threading.Event()

        # streaming state
        self.quotes: dict[int, Quote] = {}
        self._lock = threading.Lock()

    # ---- helpers ----
    def next_id(self) -> int:
        with self._req_id_lock:
            self._next_req_id += 1
            return self._next_req_id

    def _new_cd_request(self) -> tuple[int, threading.Event]:
        rid = self.next_id()
        ev = threading.Event()
        self._cd_results[rid] = []
        self._cd_done[rid] = ev
        return rid, ev

    # ---- EWrapper overrides ----
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (2104, 2106, 2158, 2107, 2103, 2105, 2119, 2150):
            return
        logging.warning("IB error reqId=%s code=%s %s", reqId, errorCode, errorString)

    def contractDetails(self, reqId, contractDetails):
        if reqId in self._cd_results:
            self._cd_results[reqId].append(contractDetails)

    def contractDetailsEnd(self, reqId):
        ev = self._cd_done.get(reqId)
        if ev:
            ev.set()

    def securityDefinitionOptionParameter(
        self, reqId, exchange, underlyingConId, tradingClass,
        multiplier, expirations, strikes,
    ):
        self._opt_params.append({
            "exchange": exchange,
            "underlyingConId": underlyingConId,
            "tradingClass": tradingClass,
            "multiplier": multiplier,
            "expirations": sorted(expirations),
            "strikes": sorted(strikes),
        })

    def securityDefinitionOptionParameterEnd(self, reqId):
        self._opt_params_done.set()

    def tickPrice(self, reqId, tickType, price, attrib):
        with self._lock:
            q = self.quotes.get(reqId)
            if not q:
                return
            if tickType == TICK_BID:
                q.bid = price
            elif tickType == TICK_ASK:
                q.ask = price
            elif tickType == TICK_LAST:
                q.last = price
            q.last_update = time.time()

    def tickSize(self, reqId, tickType, size):
        with self._lock:
            q = self.quotes.get(reqId)
            if not q:
                return
            if tickType == TICK_VOLUME:
                q.volume = int(size)
            elif tickType == TICK_FUT_OI:
                q.open_interest = int(size)
            elif tickType in (TICK_OPTION_CALL_OI, TICK_OPTION_PUT_OI):
                q.open_interest = int(size)
            q.last_update = time.time()

    def tickOptionComputation(
        self, reqId, tickType, tickAttrib, impliedVol, delta, optPrice,
        pvDividend, gamma, vega, theta, undPrice,
    ):
        with self._lock:
            q = self.quotes.get(reqId)
            if not q:
                return
            if impliedVol is not None and impliedVol > 0:
                q.iv = impliedVol
            if delta is not None:
                q.delta = delta
            if gamma is not None:
                q.gamma = gamma
            if vega is not None:
                q.vega = vega
            if theta is not None:
                q.theta = theta
            q.last_update = time.time()


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------
def discover_cl_futures_all(app: IBStream) -> list[ContractDetails]:
    """Pull every listed CL future on NYMEX (full forward curve)."""
    template = Contract()
    template.symbol = "CL"
    template.secType = "FUT"
    template.exchange = "NYMEX"
    template.currency = "USD"
    template.includeExpired = False

    rid, ev = app._new_cd_request()
    app.reqContractDetails(rid, template)
    if not ev.wait(timeout=15):
        raise RuntimeError("Timed out discovering CL futures")
    return app._cd_results.pop(rid, [])


def details_to_contract_info(details: list[ContractDetails]) -> list[ContractInfo]:
    """Convert IB ContractDetails list to ContractInfo objects for the rolling window."""
    out = []
    for d in details:
        c = d.contract
        out.append(ContractInfo(
            symbol=c.symbol,
            local_symbol=c.localSymbol,
            expiry=c.lastTradeDateOrContractMonth,
            con_id=c.conId,
            exchange=c.exchange,
        ))
    return out


def discover_option_chain_for(app: IBStream, future_con_id: int, exchange: str = "NYMEX") -> list[dict]:
    """Pull all option chain entries for a given future conId."""
    app._opt_params.clear()
    app._opt_params_done.clear()
    rid = app.next_id()
    app.reqSecDefOptParams(
        rid,
        underlyingSymbol="CL",
        futFopExchange=exchange,
        underlyingSecType="FUT",
        underlyingConId=future_con_id,
    )
    if not app._opt_params_done.wait(timeout=15):
        logging.warning("Timeout fetching option chain for conId=%s", future_con_id)
        return []
    return list(app._opt_params)


def build_fop_contracts_around_atm(
    chain_entries: list[dict],
    wanted_classes: set[str],
    atm_price: float,
    strike_window: int,
    expiry_filter: Optional[set[str]] = None,
) -> list[Contract]:
    """Construct FOP Contracts around ATM for the requested trading classes.

    `expiry_filter` (if given) restricts to those exact expiry strings.
    """
    contracts: list[Contract] = []
    for entry in chain_entries:
        if wanted_classes and entry["tradingClass"] not in wanted_classes:
            continue
        strikes_sorted = sorted(entry["strikes"])
        idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - atm_price))
        lo = max(0, idx - strike_window)
        hi = min(len(strikes_sorted), idx + strike_window + 1)
        local_strikes = strikes_sorted[lo:hi]

        for expiry in entry["expirations"]:
            if expiry_filter is not None and expiry not in expiry_filter:
                continue
            for strike in local_strikes:
                for right in ("C", "P"):
                    c = Contract()
                    c.symbol = "CL"
                    c.secType = "FOP"
                    c.exchange = entry["exchange"] or "NYMEX"
                    c.currency = "USD"
                    c.lastTradeDateOrContractMonth = expiry
                    c.strike = strike
                    c.right = right
                    c.multiplier = entry["multiplier"]
                    c.tradingClass = entry["tradingClass"]
                    contracts.append(c)
    return contracts


# ---------------------------------------------------------------------------
# Stream daemon
# ---------------------------------------------------------------------------
class StreamDaemon:
    def __init__(self, settings: dict, db_path: str, snapshot_dir: str) -> None:
        self.settings = settings
        self.db_path = db_path
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        self.app = IBStream()
        self.api_thread: Optional[threading.Thread] = None
        self.window: Optional[ActiveWindow] = None

        # local symbol → req_id of its market data subscription
        self.fut_sub: dict[str, int] = {}
        # (class, expiry, strike, right) → req_id
        self.fop_sub: dict[tuple, int] = {}

        self._stop = threading.Event()
        self._setup_signals()

    def _setup_signals(self) -> None:
        # Only safe to install in the main thread (signal.signal raises ValueError otherwise).
        # When running under daemons.main, the parent installs its own handlers.
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._on_signal)
                signal.signal(signal.SIGTERM, self._on_signal)
            except ValueError:
                pass

    def _on_signal(self, signum, frame) -> None:
        logging.info("Received signal %s, shutting down...", signum)
        self._stop.set()

    # ---- IB lifecycle ----
    def connect(self) -> None:
        host = self.settings["ib"]["host"]
        port = self.settings["ib"]["port"]
        client_id = self.settings["ib"]["client_id_stream"]
        logging.info("Connecting to IB at %s:%s as client_id=%s", host, port, client_id)
        self.app.connect(host, port, client_id)
        self.api_thread = threading.Thread(target=self.app.run, daemon=True)
        self.api_thread.start()
        time.sleep(1.5)

    def disconnect(self) -> None:
        try:
            for rid in list(self.fut_sub.values()) + list(self.fop_sub.values()):
                try:
                    self.app.cancelMktData(rid)
                except Exception:
                    pass
            self.app.disconnect()
        except Exception:
            pass

    # ---- rolling window ----
    def maybe_rebuild_window(self) -> None:
        rebuild_hour = int(self.settings["rolling_window"]["rebuild_hour_et"])
        if not needs_rebuild(self.window, datetime.now(), rebuild_hour):
            return

        logging.info("Rebuilding rolling window of active contracts...")
        try:
            details = discover_cl_futures_all(self.app)
        except Exception as e:
            logging.error("Failed to discover CL futures: %s", e)
            return

        all_contracts = details_to_contract_info(details)
        try:
            open_locals = get_open_position_local_symbols(self.db_path)
        except Exception:
            open_locals = set()

        new_window = compute_active_window(
            discovered_contracts=all_contracts,
            open_position_locals=open_locals,
            n_months_ahead=int(self.settings["rolling_window"]["n_months_ahead"]),
            drop_when_dte_le=int(self.settings["rolling_window"]["drop_when_dte_le"]),
            today=date.today(),
        )

        diff = diff_windows(self.window, new_window)
        logging.info(
            "Window: tradeable=%s markable=%s | added=%s removed=%s",
            [c.local_symbol for c in new_window.tradeable],
            [c.local_symbol for c in new_window.markable],
            diff["added"], diff["removed"],
        )

        # Apply the diff to subscriptions. On first build (self.window is None),
        # _apply_subscription_diff falls back to subscribing every tradeable
        # contract since old_locals will be empty.
        from core.rolling_window import ActiveWindow as _AW
        prior = self.window or _AW(
            tradeable=[],
            markable=[],
            computed_at=datetime.now(),
            n_months_ahead=new_window.n_months_ahead,
            drop_when_dte_le=new_window.drop_when_dte_le,
        )
        self._apply_subscription_diff(prior, new_window)

        self.window = new_window

        # Persist
        try:
            write_active_contracts(self.db_path, new_window)
            write_roll_event(self.db_path, diff["added"], diff["removed"], "scheduled rebuild")
            if diff["added"] or diff["removed"]:
                write_commentary(
                    self.db_path,
                    f"Rolled window: +{diff['added']} -{diff['removed']}",
                    level="info",
                    topic="market",
                )
        except Exception as e:
            logging.warning("Failed to persist active contracts / roll event: %s", e)

    def _apply_subscription_diff(self, old: ActiveWindow, new: ActiveWindow) -> None:
        old_locals = {c.local_symbol for c in old.tradeable}
        new_locals = {c.local_symbol for c in new.tradeable}

        # Cancel removed futures
        for sym in old_locals - new_locals:
            rid = self.fut_sub.pop(sym, None)
            if rid is not None:
                try:
                    self.app.cancelMktData(rid)
                    self.app.quotes.pop(rid, None)
                    logging.info("Cancelled fut subscription %s", sym)
                except Exception:
                    pass
            # Cancel any FOPs whose underlying is this dropped future
            to_cancel = [k for k, v in list(self.fop_sub.items())
                         if self.app.quotes.get(v) and self.app.quotes[v].underlying_local_symbol == sym]
            for k in to_cancel:
                rid = self.fop_sub.pop(k)
                try:
                    self.app.cancelMktData(rid)
                    self.app.quotes.pop(rid, None)
                except Exception:
                    pass
            logging.info("Cancelled %d FOPs whose underlying was %s", len(to_cancel), sym)

        # Subscribe added futures + their option chains
        added_locals = new_locals - old_locals
        if added_locals:
            new_contracts_by_local = {c.local_symbol: c for c in new.tradeable}
            for sym in added_locals:
                ci = new_contracts_by_local[sym]
                self._subscribe_future(ci)
                self._subscribe_chain_for(ci)

    # ---- subscriptions ----
    def _subscribe_future(self, ci: ContractInfo) -> None:
        c = Contract()
        c.symbol = ci.symbol
        c.secType = "FUT"
        c.exchange = ci.exchange
        c.currency = "USD"
        c.localSymbol = ci.local_symbol
        rid = self.app.next_id()
        q = Quote(
            sec_type="FUT",
            symbol=ci.symbol,
            local_symbol=ci.local_symbol,
            expiry=ci.expiry,
        )
        with self.app._lock:
            self.app.quotes[rid] = q
        self.fut_sub[ci.local_symbol] = rid
        self.app.reqMktData(rid, c, GENERIC_TICKS_FUTURE, False, False, [])
        logging.info("Subscribed FUT %s (rid=%s)", ci.local_symbol, rid)

    def _wait_for_atm(self, local_symbol: str, timeout: float = 8.0) -> float:
        """Wait briefly for the future's first quote to use as the ATM reference."""
        rid = self.fut_sub.get(local_symbol)
        if rid is None:
            return float("nan")
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.app._lock:
                q = self.app.quotes.get(rid)
                if q:
                    if q.last == q.last and q.last > 0:
                        return float(q.last)
                    if q.bid == q.bid and q.ask == q.ask and q.bid > 0 and q.ask > 0:
                        return (float(q.bid) + float(q.ask)) / 2
            time.sleep(0.25)
        return float("nan")

    def _subscribe_chain_for(self, ci: ContractInfo) -> None:
        """Discover and subscribe LO option chains for the given underlying future."""
        chain = discover_option_chain_for(self.app, ci.con_id, ci.exchange)
        if not chain:
            logging.warning("No chain returned for %s", ci.local_symbol)
            return

        atm = self._wait_for_atm(ci.local_symbol)
        if atm != atm:  # NaN
            logging.warning("Could not get ATM reference for %s; defaulting to 75.0", ci.local_symbol)
            atm = 75.0

        wanted_classes = set(self.settings["stream"]["classes"])
        strike_window = int(self.settings["stream"]["strike_window"])
        weekly_window = int(self.settings["stream"]["weekly_strike_window"])
        max_lines = int(self.settings["stream"]["max_lines"])

        # Build per-class with the correct window
        all_contracts: list[Contract] = []
        for entry in chain:
            tc = entry["tradingClass"]
            if tc not in wanted_classes:
                continue
            window = strike_window if tc == "LO" else weekly_window
            built = build_fop_contracts_around_atm(
                [entry], {tc}, atm, window, expiry_filter=None,
            )
            all_contracts.extend(built)

        # Respect max_lines budget across all subscriptions
        used = len(self.fut_sub) + len(self.fop_sub)
        budget = max_lines - used
        if budget <= 0:
            logging.warning("Max lines budget exhausted; skipping FOP subscriptions for %s", ci.local_symbol)
            return
        if len(all_contracts) > budget:
            logging.warning("Truncating FOP contracts for %s: %d → %d", ci.local_symbol, len(all_contracts), budget)
            all_contracts = all_contracts[:budget]

        for opt in all_contracts:
            key = (opt.tradingClass, opt.lastTradeDateOrContractMonth, float(opt.strike), opt.right)
            if key in self.fop_sub:
                continue
            rid = self.app.next_id()
            q = Quote(
                sec_type="FOP",
                symbol="CL",
                trading_class=opt.tradingClass,
                expiry=opt.lastTradeDateOrContractMonth,
                strike=float(opt.strike),
                right=opt.right,
                underlying_local_symbol=ci.local_symbol,
            )
            with self.app._lock:
                self.app.quotes[rid] = q
            self.fop_sub[key] = rid
            self.app.reqMktData(rid, opt, GENERIC_TICKS_OPTION, False, False, [])
        logging.info("Subscribed %d FOPs for %s (atm=%.2f)", len(all_contracts), ci.local_symbol, atm)

    # ---- snapshot ----
    def snapshot_to_parquet(self) -> None:
        ts = datetime.now()
        with self.app._lock:
            # Build {future local_symbol -> last/mid price}
            fut_prices: dict[str, float] = {}
            for q in self.app.quotes.values():
                if q.sec_type != "FUT" or not q.local_symbol:
                    continue
                px = float("nan")
                if q.last == q.last and q.last > 0:
                    px = q.last
                elif q.bid == q.bid and q.ask == q.ask and q.bid > 0 and q.ask > 0:
                    px = (q.bid + q.ask) / 2
                if px == px:
                    fut_prices[q.local_symbol] = px
            front_last = next(iter(fut_prices.values()), float("nan"))

            rows = []
            for q in self.app.quotes.values():
                if q.sec_type == "FUT":
                    und = fut_prices.get(q.local_symbol, float("nan"))
                    und_sym = q.local_symbol
                else:
                    und = fut_prices.get(q.underlying_local_symbol, float("nan"))
                    if und != und:
                        und = front_last
                    und_sym = q.underlying_local_symbol

                mid = float("nan")
                if q.bid == q.bid and q.ask == q.ask and q.bid > 0 and q.ask > 0:
                    mid = (q.bid + q.ask) / 2

                rows.append({
                    "ts": ts,
                    "sec_type": q.sec_type,
                    "trading_class": q.trading_class,
                    "expiry": q.expiry,
                    "strike": float(q.strike),
                    "right": q.right,
                    "local_symbol": q.local_symbol,
                    "underlying_local_symbol": und_sym,
                    "bid": float(q.bid),
                    "ask": float(q.ask),
                    "mid": mid,
                    "last": float(q.last),
                    "volume": int(q.volume),
                    "open_interest": int(q.open_interest),
                    "iv": float(q.iv),
                    "delta": float(q.delta),
                    "gamma": float(q.gamma),
                    "vega": float(q.vega),
                    "theta": float(q.theta),
                    "underlying_last": float(und),
                })

        if not rows:
            return
        out_dir = self.snapshot_dir / f"date={ts.strftime('%Y-%m-%d')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"snap_{ts.strftime('%H%M%S')}.parquet"
        try:
            table = pa.Table.from_pylist(rows)
            pq.write_table(table, out_path, compression="zstd")
        except Exception as exc:
            logging.warning("Snapshot write failed: %s", exc)

    # ---- main loop ----
    def run(self) -> None:
        self.connect()
        try:
            self.maybe_rebuild_window()  # initial build
            interval = float(self.settings["stream"]["snapshot_interval_sec"])
            heartbeat_interval = max(60.0, interval)
            last_heartbeat = 0.0

            while not self._stop.is_set():
                self.maybe_rebuild_window()
                self.snapshot_to_parquet()

                now = time.time()
                if now - last_heartbeat > heartbeat_interval:
                    n_fut = len(self.fut_sub)
                    n_fop = len(self.fop_sub)
                    write_commentary(
                        self.db_path,
                        f"stream heartbeat: {n_fut} fut + {n_fop} fop subscriptions",
                        level="info",
                        topic="stream",
                    )
                    last_heartbeat = now

                self._stop.wait(interval)
        finally:
            self.disconnect()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def load_settings(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.toml")
    p.add_argument("--db", default=None, help="DB path override (else from settings)")
    p.add_argument("--snapshot-dir", default=None, help="snapshot dir override (else from settings)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings_path = Path(args.config)
    if not settings_path.exists():
        logging.error("Config not found: %s", settings_path)
        sys.exit(1)
    settings = load_settings(settings_path)

    db_path = args.db or settings["paths"]["db_path"]
    snapshot_dir = args.snapshot_dir or settings["paths"]["snapshot_dir"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)

    daemon = StreamDaemon(settings, db_path, snapshot_dir)
    daemon.run()


if __name__ == "__main__":
    main()
