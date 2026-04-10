"""Summarizer daemon — runs `core.summarizer.build_summary` on a schedule.

Loop:
  1. Sleep until next aligned tick (top of hour by default)
  2. build_summary() → Summary object
  3. Insert into `summaries` table
  4. If write_md_file: dump body_md to data/summaries/YYYY-MM-DD/HH.md
  5. If push_notifications: notify(headline, level, url) via interfaces/notifier.py
  6. Repeat

Run standalone:
  python -m daemons.summarizer

Or as a worker thread inside daemons/main.py (the standard way).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from core.db import transaction
from core.summarizer import Summary, build_summary


def _next_aligned_tick(interval_seconds: float, align_to_clock: bool, now: datetime) -> datetime:
    """Compute the next time we should fire."""
    if not align_to_clock:
        return now + timedelta(seconds=interval_seconds)
    # Snap to next boundary of interval_seconds since midnight
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_since_midnight = (now - midnight).total_seconds()
    n = int(seconds_since_midnight // interval_seconds)
    next_tick = midnight + timedelta(seconds=(n + 1) * interval_seconds)
    return next_tick


def persist_summary(db_path: str, summary: Summary, push_target: str | None = None) -> int:
    """Insert a summary row, return id."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO summaries (ts, period_start, period_end, headline, body_md, metrics_json, pushed, push_target)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                summary.ts.isoformat(),
                summary.period_start.isoformat(),
                summary.period_end.isoformat(),
                summary.headline,
                summary.body_md,
                json.dumps(summary.metrics, default=str),
                1 if push_target else 0,
                push_target,
            ],
        )
        return int(cur.lastrowid or 0)


def write_md_file(md_dir: str | Path, summary: Summary) -> Path:
    """Write the summary markdown to disk under date-partitioned dirs."""
    base = Path(md_dir)
    day = summary.ts.strftime("%Y-%m-%d")
    out_dir = base / day
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = summary.ts.strftime("%H%M%S") + ".md"
    out_path = out_dir / fname
    out_path.write_text(summary.body_md)
    return out_path


class SummarizerDaemon:
    def __init__(self, settings: dict, db_path: str, snapshot_dir: str) -> None:
        self.settings = settings
        self.db_path = db_path
        self.snapshot_dir = snapshot_dir
        self.cfg = settings.get("summarizer", {})
        self._stop = threading.Event()
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._on_signal)
                signal.signal(signal.SIGTERM, self._on_signal)
            except ValueError:
                pass

    def _on_signal(self, signum, frame) -> None:
        logging.info("Summarizer received signal %s, stopping...", signum)
        self._stop.set()

    def tick(self) -> Optional[Summary]:
        """Build and persist one summary. Return the Summary or None if disabled."""
        if not self.cfg.get("enabled", True):
            return None

        window_hours = float(self.cfg.get("window_hours", 1.0))
        summary = build_summary(self.db_path, self.snapshot_dir, window_hours=window_hours)

        # Optional LLM narrative — gated by summarizer.include_llm_narrative
        if self.cfg.get("include_llm_narrative", False):
            try:
                from agents.narrator import narrate_summary as _narrate
                monthly_budget = float(
                    self.settings.get("anthropic", {}).get("monthly_budget", 200.0)
                )
                narrative = _narrate(summary, self.db_path, monthly_budget=monthly_budget)
                if narrative:
                    # Prepend narrative as the first section of body_md
                    header_end = summary.body_md.find("\n\n")
                    if header_end == -1:
                        summary.body_md = summary.body_md + "\n\n## Narrative\n" + narrative
                    else:
                        summary.body_md = (
                            summary.body_md[:header_end]
                            + "\n\n## Narrative\n"
                            + narrative
                            + summary.body_md[header_end:]
                        )
                    # Use the narrative as the push headline if it's short enough
                    first_sentence = narrative.split(". ", 1)[0].strip()
                    if 20 <= len(first_sentence) <= 180:
                        summary.headline = first_sentence.rstrip(".")
            except Exception as e:
                logging.warning("narrate_summary failed (continuing without narrative): %s", e)

        # Decide whether to push
        push_target: str | None = None
        push_threshold = self.cfg.get("push_threshold", "all")
        push_enabled = bool(self.cfg.get("push_notifications", False))
        should_push = push_enabled and (
            push_threshold == "all" or summary.is_important
        )
        if should_push:
            try:
                from interfaces.notifier import notify
                level: "Literal['info','warn','alert','critical']" = (
                    "alert" if summary.is_important else "info"
                )
                ok = notify(summary.headline, level=level, title="Oil Trader hourly")
                push_target = "ntfy" if ok else None
            except Exception as e:
                logging.warning("notify failed: %s", e)

        # Persist
        try:
            sid = persist_summary(self.db_path, summary, push_target)
            logging.info("Summary #%s: %s", sid, summary.headline)
        except Exception as e:
            logging.exception("persist_summary failed: %s", e)
            return summary

        # Markdown file
        if self.cfg.get("write_md_file", True):
            try:
                md_path = write_md_file(self.cfg.get("md_dir", "./data/summaries"), summary)
                logging.info("Summary md → %s", md_path)
            except Exception as e:
                logging.warning("write_md_file failed: %s", e)

        return summary

    def run(self) -> None:
        if not self.cfg.get("enabled", True):
            logging.info("Summarizer disabled in settings; worker idle")
            while not self._stop.is_set():
                self._stop.wait(60)
            return

        interval = float(self.cfg.get("interval_seconds", 3600))
        align = bool(self.cfg.get("align_to_clock", True))
        logging.info(
            "Summarizer starting (interval=%.0fs, align=%s, push=%s)",
            interval, align, self.cfg.get("push_notifications", False),
        )

        # Wait until first aligned tick
        first = _next_aligned_tick(interval, align, datetime.now())
        wait_s = max(0, (first - datetime.now()).total_seconds())
        logging.info("First summary at %s (in %.0fs)", first.isoformat(timespec="seconds"), wait_s)
        if self._stop.wait(wait_s):
            return

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                logging.exception("summarizer tick failed: %s", e)
            # Compute the next tick from "now" so we don't drift
            nxt = _next_aligned_tick(interval, align, datetime.now())
            wait_s = max(1, (nxt - datetime.now()).total_seconds())
            self._stop.wait(wait_s)


def load_settings(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.toml")
    p.add_argument("--db", default=None)
    p.add_argument("--snapshot-dir", default=None)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--once", action="store_true",
                   help="run a single tick and exit (useful for cron / manual catch-up)")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings(Path(args.config))
    db_path = args.db or settings["paths"]["db_path"]
    snapshot_dir = args.snapshot_dir or settings["paths"]["snapshot_dir"]

    daemon = SummarizerDaemon(settings, db_path, snapshot_dir)
    if args.once:
        s = daemon.tick()
        if s:
            print(f"\n{s.body_md}\n")
    else:
        daemon.run()


if __name__ == "__main__":
    main()
