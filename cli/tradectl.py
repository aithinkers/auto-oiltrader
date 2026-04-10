"""tradectl — single CLI entry point for the trading system.

Usage:
  tradectl status                 Show system mode, capital, daily P&L
  tradectl positions              List open positions
  tradectl recommendations [--pending]
  tradectl mode <name>            Set mode (paper|draft|live|halt)
  tradectl halt                   Emergency stop (mode → halt)
  tradectl unhalt                 Resume (mode → paper)
  tradectl cash                   Show cash row
  tradectl init-db                Initialize the DB schema and seed
  tradectl reconcile              Run the IB reconciler
  tradectl approve <rec_id>
  tradectl reject <rec_id> [--reason TEXT]
  tradectl observe <text>         Submit a user observation
  tradectl costs [--month]        Show cost ledger summary
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from core.db import (
    approve_draft_recommendation,
    get_conn,
    get_current_cash,
    init_schema,
    reject_draft_recommendation,
    transaction,
)


app = typer.Typer(help="Autonomous Oil Trader CLI")
console = Console()


def _db_path() -> str:
    return os.environ.get("TRADER_DB_PATH", "./data/trader.db")


@app.command()
def status() -> None:
    """Show system mode, capital, daily P&L."""
    try:
        cash = get_current_cash(_db_path())
    except Exception as e:
        console.print(f"[red]✗ DB not initialized: {e}[/red]")
        console.print("Run [yellow]tradectl init-db[/yellow] first.")
        raise typer.Exit(1)

    t = Table(title="System Status", show_header=False)
    t.add_column("key", style="cyan")
    t.add_column("value")
    t.add_row("Mode", f"[bold]{cash['mode']}[/bold]")
    t.add_row("Account", cash["account"])
    t.add_row("Starting capital", f"${cash['starting_capital']:,.2f}")
    t.add_row("Current balance", f"${cash['current_balance']:,.2f}")
    t.add_row("High watermark", f"${cash['high_watermark']:,.2f}")
    t.add_row("Withdrawals", f"${cash['withdrawals']:,.2f}")
    t.add_row("Daily P&L", f"${cash['daily_pnl']:,.2f}")
    t.add_row("Daily loss halt", f"-${cash['daily_loss_halt']:,.2f}")
    console.print(t)


@app.command()
def positions() -> None:
    """List open positions."""
    conn = get_conn(_db_path())
    rows = conn.execute(
        "SELECT id, structure, qty, open_debit, status, ts_opened FROM positions WHERE status='open' ORDER BY ts_opened"
    ).fetchall()
    if not rows:
        console.print("[dim]No open positions[/dim]")
        return
    t = Table(title="Open Positions")
    t.add_column("id")
    t.add_column("structure")
    t.add_column("qty", justify="right")
    t.add_column("open debit", justify="right")
    t.add_column("status")
    t.add_column("opened")
    from core.timefmt import fmt_local
    for r in rows:
        t.add_row(str(r[0]), r[1], str(r[2]), f"{float(r[3]):.4f}", r[4], fmt_local(r[5]))
    console.print(t)


@app.command()
def recommendations(
    pending: bool = typer.Option(False, "--pending", help="Show only pending recommendations"),
) -> None:
    """List recent recommendations."""
    conn = get_conn(_db_path())
    if pending:
        rows = conn.execute(
            """
            SELECT id, ts, source, strategy_id, structure, status, target_debit,
                   approved_by, rejection_reason
            FROM recommendations
            WHERE status = 'pending'
            ORDER BY ts DESC
            LIMIT 100
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, ts, source, strategy_id, structure, status, target_debit,
                   approved_by, rejection_reason
            FROM recommendations
            ORDER BY ts DESC
            LIMIT 100
            """
        ).fetchall()

    if not rows:
        console.print("[dim]No recommendations found[/dim]")
        return

    from core.timefmt import fmt_local

    t = Table(title="Recommendations")
    t.add_column("id", justify="right")
    t.add_column("time")
    t.add_column("strategy")
    t.add_column("structure")
    t.add_column("status")
    t.add_column("debit", justify="right")
    t.add_column("decision")
    for row in rows:
        decision = row["approved_by"] or row["rejection_reason"] or ""
        t.add_row(
            str(row["id"]),
            fmt_local(row["ts"]),
            row["strategy_id"] or row["source"],
            row["structure"],
            row["status"],
            f"{float(row['target_debit'] or 0.0):.4f}",
            decision,
        )
    console.print(t)


@app.command()
def cash() -> None:
    """Show full cash row."""
    from core.timefmt import fmt_local
    cash_row = get_current_cash(_db_path())
    for k, v in cash_row.items():
        if k == "ts" and v:
            v = fmt_local(v)
        console.print(f"  [cyan]{k}[/cyan]: {v}")


@app.command()
def mode(new_mode: str = typer.Argument(...)) -> None:
    """Set mode (paper|draft|live|halt)."""
    if new_mode not in ("paper", "draft", "live", "halt"):
        console.print(f"[red]Invalid mode: {new_mode}[/red]")
        raise typer.Exit(1)
    from core.db import utc_now_iso
    with transaction(_db_path()) as conn:
        cash_row = conn.execute("SELECT * FROM cash ORDER BY ts DESC LIMIT 1").fetchone()
        if cash_row is None:
            console.print("[red]No cash row[/red]")
            raise typer.Exit(1)
        cash_dict = {k: cash_row[k] for k in cash_row.keys()}
        cash_dict["mode"] = new_mode
        cash_dict["ts"] = utc_now_iso()
        cash_dict["notes"] = f"mode change to {new_mode}"
        col_names = ", ".join(cash_dict.keys())
        placeholders = ", ".join(["?"] * len(cash_dict))
        conn.execute(
            f"INSERT INTO cash ({col_names}) VALUES ({placeholders})",
            list(cash_dict.values()),
        )
    console.print(f"[green]✓ Mode set to {new_mode}[/green]")


@app.command()
def halt() -> None:
    """Emergency stop — sets mode to halt."""
    mode("halt")
    console.print("[bold red]🛑 SYSTEM HALTED[/bold red]")


@app.command()
def unhalt() -> None:
    """Resume by setting mode to paper."""
    mode("paper")
    console.print("[green]✓ Resumed in paper mode[/green]")


@app.command(name="init-db")
def init_db(
    force: bool = typer.Option(
        False, "--force",
        help="DESTRUCTIVE: delete the existing DB file and start over. "
             "Use only when you actually want to wipe history.",
    ),
) -> None:
    """Initialize the DB schema. Non-destructive by default — safe to re-run.

    Creates the file if it doesn't exist. If it does exist, applies any new
    schema (CREATE IF NOT EXISTS) and runs `migrate` to bring it up to the
    latest version. Existing rows are preserved.

    Use `--force` ONLY when you want to wipe all history.
    """
    db_path = Path(_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists() and force:
        # Auto-backup before destroying
        backup = db_path.with_suffix(
            f".pre-force-{__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        )
        import shutil
        shutil.copy2(db_path, backup)
        console.print(f"[yellow]Backed up existing DB → {backup}[/yellow]")
        db_path.unlink()
        # Also clean WAL/SHM
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
        console.print(f"[red]✗ Wiped existing DB[/red]")

    is_new = not db_path.exists()
    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    init_schema(db_path, schema_path)
    if is_new:
        console.print(f"[green]✓ Created new DB at {db_path}[/green]")
    else:
        console.print(f"[green]✓ Schema applied (existing data preserved) at {db_path}[/green]")

    # Run any pending migrations
    _apply_migrations(db_path)

    # Seed only if cash table is empty (preserves existing balance/history)
    import sqlite3
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        cash_count = conn.execute("SELECT COUNT(*) FROM cash").fetchone()[0]
        if cash_count == 0:
            seed_path = Path(__file__).parent.parent / "db" / "seed.sql"
            conn.executescript(seed_path.read_text())
            _apply_settings_to_cash_row(conn)
            console.print("[dim]Seeded initial cash row from settings.toml[/dim]")
        else:
            console.print(f"[dim]Cash table has {cash_count} row(s); seed skipped (use `tradectl reseed-cash` to re-apply settings.toml)[/dim]")
    finally:
        conn.close()


def _apply_settings_to_cash_row(conn) -> None:
    """Overwrite the latest cash row with values from settings.toml [capital]."""
    settings_path = Path(__file__).parent.parent / "config" / "settings.toml"
    if not settings_path.exists():
        return
    import tomllib
    with open(settings_path, "rb") as f:
        cfg = tomllib.load(f)
    cap = cfg.get("capital", {})
    mode_default = cfg.get("mode", {}).get("default", "paper")
    starting = float(cap.get("starting", 20000))
    halt_amt = float(cap.get("daily_loss_halt", 1000))
    conn.execute(
        """
        UPDATE cash
        SET starting_capital = ?, current_balance = ?, high_watermark = ?,
            daily_loss_halt  = ?, mode = ?, notes = 'init from settings.toml'
        WHERE rowid = (SELECT rowid FROM cash ORDER BY ts DESC LIMIT 1)
        """,
        [starting, starting, starting, halt_amt, mode_default],
    )
    console.print(
        f"[dim]Applied settings.toml: starting=${starting:,.0f}, "
        f"halt=-${halt_amt:,.0f}, mode={mode_default}[/dim]"
    )


def _apply_migrations(db_path: Path) -> int:
    """Run any migration files in db/migrations whose version isn't yet applied.

    Returns the number of migrations applied.
    """
    import re
    import sqlite3
    migrations_dir = Path(__file__).parent.parent / "db" / "migrations"
    if not migrations_dir.exists():
        return 0

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Make sure schema_version table exists (older DBs may not have it)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                notes      TEXT
            )
        """)
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}

        applied_count = 0
        for mig_path in sorted(migrations_dir.glob("*.sql")):
            m = re.match(r"^(\d+)_", mig_path.name)
            if not m:
                continue
            version = int(m.group(1))
            if version in applied:
                continue
            sql = mig_path.read_text()
            try:
                conn.executescript(sql)
                console.print(f"[green]✓ Applied migration {mig_path.name}[/green]")
                applied_count += 1
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                # Gracefully handle "duplicate column" when schema.sql (fresh
                # install) already created the column and the migration would
                # otherwise fail.
                if "duplicate column" in msg:
                    # Record the migration as applied so we don't retry forever
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_version (version, applied_at, notes) "
                        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
                        [version, f"skipped (column already exists): {mig_path.name}"],
                    )
                    console.print(f"[dim]· Skipped {mig_path.name} (column already present)[/dim]")
                    applied_count += 1
                    continue
                console.print(f"[red]✗ Migration {mig_path.name} failed: {e}[/red]")
                raise
            except Exception as e:
                console.print(f"[red]✗ Migration {mig_path.name} failed: {e}[/red]")
                raise

        if applied_count == 0:
            console.print("[dim]Migrations: up to date[/dim]")
        return applied_count
    finally:
        conn.close()


@app.command()
def migrate() -> None:
    """Apply pending DB migrations from db/migrations/. Non-destructive."""
    db_path = Path(_db_path())
    if not db_path.exists():
        console.print(f"[red]DB not found at {db_path}. Run `tradectl init-db` first.[/red]")
        raise typer.Exit(1)
    n = _apply_migrations(db_path)
    if n > 0:
        console.print(f"[green]✓ Applied {n} migration(s)[/green]")


@app.command()
def backup(
    out_dir: str = typer.Option("./data/backups", "--out", help="Backup directory"),
) -> None:
    """Create a timestamped backup copy of the DB.

    Uses SQLite's online backup so it's safe to run while the daemon is
    actively writing.
    """
    db_path = Path(_db_path())
    if not db_path.exists():
        console.print(f"[red]DB not found at {db_path}[/red]")
        raise typer.Exit(1)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = out / f"trader-{ts}.db"

    import sqlite3
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()

    size_mb = backup_path.stat().st_size / 1024 / 1024
    console.print(f"[green]✓ Backup → {backup_path} ({size_mb:.2f} MB)[/green]")


@app.command()
def restore(
    backup_path: str = typer.Argument(..., help="Path to a backup .db file"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation prompt"),
) -> None:
    """Restore the trader DB from a backup. DESTRUCTIVE on the current DB."""
    src = Path(backup_path)
    if not src.exists():
        console.print(f"[red]Backup not found: {src}[/red]")
        raise typer.Exit(1)
    db_path = Path(_db_path())

    if not yes:
        console.print(f"[yellow]This will replace {db_path} with {src}. Continue? [y/N][/yellow]")
        ans = input().strip().lower()
        if ans != "y":
            console.print("Aborted.")
            raise typer.Exit(0)

    # Backup current first
    if db_path.exists():
        from datetime import datetime
        save = db_path.with_suffix(f".pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
        import shutil
        shutil.copy2(db_path, save)
        console.print(f"[dim]Saved current DB → {save}[/dim]")
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()

    import shutil
    shutil.copy2(src, db_path)
    console.print(f"[green]✓ Restored DB from {src}[/green]")
    # Apply any new migrations to bring the restored DB up to current schema
    _apply_migrations(db_path)


@app.command()
def wipe(
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
) -> None:
    """DESTRUCTIVE: delete the DB and start fresh. Auto-backups before deleting."""
    db_path = Path(_db_path())
    if not db_path.exists():
        console.print("[yellow]DB doesn't exist; nothing to wipe.[/yellow]")
        raise typer.Exit(0)

    if not yes:
        console.print(f"[red]This will DELETE all data in {db_path}.[/red]")
        console.print("[yellow]An auto-backup will be saved first. Continue? [y/N][/yellow]")
        ans = input().strip().lower()
        if ans != "y":
            console.print("Aborted.")
            raise typer.Exit(0)

    # Auto-backup
    from datetime import datetime
    save = db_path.with_suffix(f".pre-wipe-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    import shutil
    shutil.copy2(db_path, save)
    console.print(f"[dim]Auto-backed up → {save}[/dim]")

    db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    console.print(f"[red]✗ Wiped {db_path}[/red]")
    console.print("[dim]Run `tradectl init-db` to recreate with a fresh schema.[/dim]")


@app.command(name="reseed-cash")
def reseed_cash() -> None:
    """Re-apply settings.toml [capital] values to the latest cash row.

    Use this when you change starting_capital or daily_loss_halt in
    settings.toml and want them reflected in the running cash row WITHOUT
    wiping the DB.
    """
    db_path = Path(_db_path())
    if not db_path.exists():
        console.print(f"[red]DB not found at {db_path}[/red]")
        raise typer.Exit(1)
    import sqlite3
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Insert a new cash row using the latest as a template, then apply settings
        conn.execute(
            """
            INSERT INTO cash (ts, account, starting_capital, current_balance, high_watermark,
                              withdrawals, mode, daily_pnl, daily_loss_halt, notes)
            SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), account, starting_capital, current_balance, high_watermark,
                   withdrawals, mode, daily_pnl, daily_loss_halt, 'pre-reseed snapshot'
            FROM cash ORDER BY ts DESC LIMIT 1
            """
        )
        _apply_settings_to_cash_row(conn)
        console.print("[green]✓ Cash row updated from settings.toml (history preserved)[/green]")
    finally:
        conn.close()


@app.command()
def costs(month: bool = typer.Option(False, "--month", help="Limit to current month")) -> None:
    """Show cost ledger summary."""
    conn = get_conn(_db_path())
    # SQLite: filter to start of current month
    where = "WHERE ts >= strftime('%Y-%m-01', 'now')" if month else ""
    rows = conn.execute(
        f"SELECT category, COUNT(*) as n, SUM(amount) as total FROM costs {where} GROUP BY category"
    ).fetchall()
    t = Table(title="Cost ledger" + (" — this month" if month else ""))
    t.add_column("category")
    t.add_column("entries", justify="right")
    t.add_column("total", justify="right")
    total = 0.0
    for cat, n, amt in rows:
        amt = float(amt or 0)
        total += amt
        t.add_row(cat, str(n), f"${amt:,.2f}")
    t.add_row("[bold]TOTAL[/bold]", "", f"[bold]${total:,.2f}[/bold]")
    console.print(t)


@app.command()
def observe(text: str) -> None:
    """Submit a user observation."""
    from datetime import datetime, timedelta, timezone
    from core.db import to_utc_iso
    now_utc = datetime.now(timezone.utc)
    with transaction(_db_path()) as conn:
        conn.execute(
            """INSERT INTO user_observations (ts, text, category, weight, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            [to_utc_iso(now_utc), text, "user", 0.6, to_utc_iso(now_utc + timedelta(hours=24))],
        )
    console.print("[green]✓ Observation recorded[/green]")


@app.command()
def approve(
    rec_id: int = typer.Argument(..., help="Recommendation ID to approve"),
    by: str = typer.Option(
        os.environ.get("USER", "tradectl"),
        "--by",
        help="Actor name to record in the audit trail",
    ),
) -> None:
    """Approve a staged draft recommendation."""
    ok, message = approve_draft_recommendation(_db_path(), rec_id, by)
    if not ok:
        console.print(f"[red]✗ {message}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓ {message}[/green]")


@app.command()
def reject(
    rec_id: int = typer.Argument(..., help="Recommendation ID to reject"),
    reason: str = typer.Option(..., "--reason", help="Reason for rejection"),
    by: str = typer.Option(
        os.environ.get("USER", "tradectl"),
        "--by",
        help="Actor name to record in the audit trail",
    ),
) -> None:
    """Reject a staged draft recommendation."""
    ok, message = reject_draft_recommendation(_db_path(), rec_id, by, reason)
    if not ok:
        console.print(f"[red]✗ {message}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓ {message}[/green]")


@app.command()
def analyze(
    position_id: int = typer.Argument(..., help="Position ID to analyze"),
) -> None:
    """LLM-powered deep-dive analysis of a single position.

    Loads the position's full history (entry thesis, marks, greeks, exit rules)
    and asks Claude (Sonnet) to produce a markdown analysis with thesis,
    current state, risk, and a recommended action.

    Requires ANTHROPIC_API_KEY in env. Cost: ~$0.02 per call.
    """
    from agents.narrator import analyze_trade
    md = analyze_trade(position_id, _db_path())
    if md is None:
        console.print(f"[red]Position {position_id} not found, or LLM call failed.[/red]")
        console.print("[dim]Check ANTHROPIC_API_KEY env var and the logs.[/dim]")
        raise typer.Exit(1)
    console.print(md)


@app.command()
def narrate(
    window_hours: float = typer.Option(1.0, "--hours", help="Window size for the summary"),
) -> None:
    """Build a fresh summary AND a Claude narrative paragraph for it.

    This is the same thing the summarizer worker produces when
    `summarizer.include_llm_narrative = true`. Useful for testing the prompt
    or pulling a one-off narrative on demand.

    Requires ANTHROPIC_API_KEY. Cost: ~$0.005 per call (Haiku).
    """
    from agents.narrator import narrate_summary
    from core.summarizer import build_summary
    snap_dir = "./data/snapshots"
    sp = Path("./config/settings.toml")
    if sp.exists():
        import tomllib
        with open(sp, "rb") as f:
            cfg = tomllib.load(f)
        snap_dir = cfg.get("paths", {}).get("snapshot_dir", snap_dir)
    s = build_summary(_db_path(), snap_dir, window_hours=window_hours)
    narrative = narrate_summary(s, _db_path())
    if narrative is None:
        console.print("[yellow]Narrative unavailable (LLM call failed or budget exceeded). Showing rules-only summary:[/yellow]\n")
        console.print(s.body_md)
        raise typer.Exit(1)
    console.print("[bold cyan]## Narrative[/bold cyan]\n")
    console.print(narrative)
    console.print("\n[bold cyan]## Structured summary[/bold cyan]\n")
    console.print(s.body_md)


@app.command()
def summary(
    now: bool = typer.Option(False, "--now", help="Build a fresh summary instead of showing the latest stored one"),
    window_hours: float = typer.Option(1.0, "--hours", help="Window size for --now mode"),
    n: int = typer.Option(1, "--n", help="How many recent stored summaries to show (ignored with --now)"),
) -> None:
    """Show recent hourly summary, or build a fresh one with --now."""
    if now:
        from core.summarizer import build_summary
        snap_dir = "./data/snapshots"
        sp = Path("./config/settings.toml")
        if sp.exists():
            import tomllib
            with open(sp, "rb") as f:
                cfg = tomllib.load(f)
            snap_dir = cfg.get("paths", {}).get("snapshot_dir", snap_dir)
        s = build_summary(_db_path(), snap_dir, window_hours=window_hours)
        console.print(s.body_md)
        console.print(f"\n[dim]Headline: {s.headline}[/dim]")
        return

    conn = get_conn(_db_path())
    rows = conn.execute(
        "SELECT id, ts, headline, body_md FROM summaries ORDER BY ts DESC LIMIT ?",
        [int(n)],
    ).fetchall()
    if not rows:
        console.print("[yellow]No summaries yet. Use --now to generate one.[/yellow]")
        return
    from core.timefmt import fmt_local
    for r in rows:
        console.print(f"\n[bold cyan]── Summary #{r['id']}  {fmt_local(r['ts'])}[/bold cyan]")
        console.print(r["body_md"])


if __name__ == "__main__":
    app()
