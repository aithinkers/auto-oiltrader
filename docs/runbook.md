# Runbook

Operational procedures for common situations.

## Emergency stop

```bash
python -m cli.tradectl halt
```

This sets `mode=halt`. Existing positions stay open and continue marking, but no new orders will be accepted. Use this any time you're unsure.

To resume:
```bash
python -m cli.tradectl unhalt        # back to paper mode
python -m cli.tradectl mode draft    # or explicitly set draft/live
```

## Starting the system

```bash
# Ensure IB TWS/Gateway is running and logged in
python -m daemons.main --log-level INFO &
streamlit run interfaces/dashboard/app.py --server.port 8511 &
```

## Stopping the system

```bash
# Find and kill processes
ps aux | grep 'daemons.main\|streamlit.*8511' | grep -v grep | awk '{print $2}' | xargs kill
```

## Daemon crashed / stopped

Check logs:
```bash
tail -100 logs/daemon.log    # or check terminal output
```

Restart:
```bash
python -m daemons.main --log-level INFO &
```

The daemon is designed to be idempotent. Restarting is safe.

## DB corruption or schema issues

```bash
# Backup current state
cp data/trader.db data/backups/trader.db.$(date +%s)

# Try applying migrations first
python -m cli.tradectl migrate

# If that doesn't fix it, reinitialize (WARNING: loses data)
python -m cli.tradectl init-db --force
```

Set up daily backups: a cron job that copies `data/trader.db` to `data/backups/` once per day.

## IB connection lost

The stream daemon retries every 30 seconds. If TWS is down for more than a few minutes:
1. Restart TWS / IB Gateway
2. Re-login if needed (TWS auto-logs out at 11:55 PM ET nightly)
3. The daemon should reconnect automatically

If positions are open and IB has been disconnected > 5 minutes, the position manager will mark them as stale and refuse to act until reconnection.

## Daily loss halt triggered

If `daily_pnl <= -$1000` (configurable), the system automatically sets `mode=halt`. To resume the next trading day:

```bash
python -m cli.tradectl status      # confirm mode=halt
python -m cli.tradectl unhalt      # resume in paper mode
```

**Investigate before resuming.** A daily loss halt is a strong signal that something is wrong.

## Unknown position appeared in IB

The reconciler detects positions in IB that aren't in the DB and refuses to proceed. Manually decide:

1. **Adopt it**: insert a matching row into `positions` table
2. **Close it**: cancel via TWS UI, then restart daemons

Never auto-adopt -- that's how things go wrong.

## Strategy is losing money

1. Check the dashboard **Strategies** page for per-strategy P&L
2. Disable the strategy in `config/strategies.yaml` (`enabled: false`)
3. Restart the daemon -- existing positions continue marking but no new entries
4. The next weekly findings report will explain what happened

## LLM budget exceeded

If monthly LLM cost exceeds the cap, agents halt automatically:

```bash
python -m cli.tradectl costs --month
```

To raise the cap: edit `config/settings.toml` `[anthropic] monthly_budget`, update your Anthropic console cap, and restart.

## Schema migration

When upgrading to a new version with schema changes:

```bash
python -m cli.tradectl halt         # stop trading
# kill the daemon
python -m cli.tradectl migrate      # apply new migrations
# restart the daemon
```

Migrations are idempotent -- running `migrate` on an up-to-date DB is safe.

## Restoring from clean state

```bash
python -m cli.tradectl halt
# Stop all processes
cp data/trader.db data/backups/trader.db.$(date +%s)
python -m cli.tradectl init-db --force
python -m cli.tradectl migrate
# Restart processes
```
