"""Main entry point — runs all daemons as worker threads in a single process.

DuckDB only allows one OS process to hold a write lock on the file at a time,
so the cleanest architecture is to run the stream daemon, position manager,
and trader daemon as worker threads inside one Python process. They share a
single DuckDB connection (cached in core.db).

Threads:
  - stream_thread: ticks every snapshot_interval_sec, writes parquet snapshots
                   and updates DB tables (commentary, active_contracts)
  - trader_thread: ticks every snapshot_interval_sec, evaluates strategies
                   and processes recommendations
  - position_thread: ticks every snapshot_interval_sec, marks positions and
                     emits closing recommendations

Each worker has independent error handling — one thread crashing does not
take down the others. SIGINT / SIGTERM cleanly stops all threads.

Run:
    python -m daemons.main --log-level INFO

Or via systemd / launchd. This replaces individual daemon entry points
when running locally.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
import tomllib
from pathlib import Path
from typing import Optional

from daemons.position_manager import PositionManager
from daemons.stream_daemon import StreamDaemon
from daemons.summarizer import SummarizerDaemon
from daemons.trader_daemon import TraderDaemon


_STOP = threading.Event()


def _on_signal(signum, frame) -> None:
    logging.info("Received signal %s, stopping all workers...", signum)
    _STOP.set()


def load_settings(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def stream_worker(daemon: StreamDaemon) -> None:
    """Stream worker — manages IB connection and snapshot loop."""
    try:
        daemon.connect()
        # Initial rolling window build + subscriptions
        daemon.maybe_rebuild_window()
        interval = float(daemon.settings["stream"]["snapshot_interval_sec"])
        while not _STOP.is_set():
            try:
                daemon.maybe_rebuild_window()
                daemon.snapshot_to_parquet()
            except Exception as e:
                logging.exception("stream tick failed: %s", e)
            _STOP.wait(interval)
    finally:
        daemon.disconnect()
        logging.info("stream worker stopped")


def trader_worker(daemon: TraderDaemon) -> None:
    """Trader worker — strategy evaluation and order processing."""
    daemon.load_strategies()
    interval = float(daemon.settings["stream"]["snapshot_interval_sec"])
    # Stagger slightly so we don't all hit the DB at the same instant
    _STOP.wait(5)
    while not _STOP.is_set():
        try:
            daemon.tick()
        except Exception as e:
            logging.exception("trader tick failed: %s", e)
        _STOP.wait(interval)
    logging.info("trader worker stopped")


def position_worker(daemon: PositionManager) -> None:
    """Position manager worker — marks and exit rule evaluation."""
    interval = float(daemon.settings["stream"]["snapshot_interval_sec"])
    _STOP.wait(10)  # let stream + trader settle first
    while not _STOP.is_set():
        try:
            daemon.tick()
        except Exception as e:
            logging.exception("position tick failed: %s", e)
        _STOP.wait(interval)
    logging.info("position worker stopped")


def summarizer_worker(daemon: SummarizerDaemon) -> None:
    """Summarizer worker — periodic catch-up digest."""
    cfg = daemon.settings.get("summarizer", {})
    if not cfg.get("enabled", True):
        logging.info("Summarizer disabled in settings")
        return
    interval = float(cfg.get("interval_seconds", 3600))
    align = bool(cfg.get("align_to_clock", True))
    from daemons.summarizer import _next_aligned_tick
    from datetime import datetime

    # Wait for first aligned tick
    first = _next_aligned_tick(interval, align, datetime.now())
    wait_s = max(0, (first - datetime.now()).total_seconds())
    logging.info("First summary at %s (in %.0fs)", first.isoformat(timespec="seconds"), wait_s)
    if _STOP.wait(wait_s):
        return

    while not _STOP.is_set():
        try:
            daemon.tick()
        except Exception as e:
            logging.exception("summarizer tick failed: %s", e)
        nxt = _next_aligned_tick(interval, align, datetime.now())
        wait_s = max(1, (nxt - datetime.now()).total_seconds())
        _STOP.wait(wait_s)
    logging.info("summarizer worker stopped")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.toml")
    p.add_argument("--db", default=None)
    p.add_argument("--snapshot-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings(Path(args.config))
    db_path = args.db or settings["paths"]["db_path"]
    snapshot_dir = args.snapshot_dir or settings["paths"]["snapshot_dir"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Path(snapshot_dir).mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    stream = StreamDaemon(settings, db_path, snapshot_dir)
    trader = TraderDaemon(settings, db_path, snapshot_dir)
    position = PositionManager(settings, db_path, snapshot_dir)
    summarizer = SummarizerDaemon(settings, db_path, snapshot_dir)

    # Each daemon also installs its own signal handlers; override with ours.
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    threads = [
        threading.Thread(target=stream_worker, args=(stream,), name="stream", daemon=False),
        threading.Thread(target=trader_worker, args=(trader,), name="trader", daemon=False),
        threading.Thread(target=position_worker, args=(position,), name="position", daemon=False),
        threading.Thread(target=summarizer_worker, args=(summarizer,), name="summarizer", daemon=False),
    ]

    for t in threads:
        t.start()
    logging.info("All workers started: %s", [t.name for t in threads])

    try:
        while not _STOP.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _STOP.set()

    logging.info("Waiting for workers to stop...")
    for t in threads:
        t.join(timeout=15)
        if t.is_alive():
            logging.warning("Worker %s did not stop within 15s", t.name)
    logging.info("All workers stopped")


if __name__ == "__main__":
    main()
