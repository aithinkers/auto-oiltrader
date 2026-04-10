-- Migration 003: entry context for enhanced exit rules.
--
-- Adds two nullable columns to positions:
--   entry_atm_iv       — ATM IV at the time this position was opened
--   entry_underlying   — underlying futures price at the time this position was opened
--
-- These are needed by the vol-crush and short-strike-tested exit rules in
-- core.risk.evaluate_exit. Existing rows have NULL, which means those rules
-- are skipped for pre-migration positions (safe default).

-- SQLite's ALTER TABLE ADD COLUMN does not support IF NOT EXISTS, so we
-- guard by checking schema_version. The migration is only applied if its
-- version isn't already recorded.

ALTER TABLE positions ADD COLUMN entry_atm_iv REAL;
ALTER TABLE positions ADD COLUMN entry_underlying REAL;

INSERT OR IGNORE INTO schema_version (version, applied_at, notes)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'add entry_atm_iv + entry_underlying for enhanced exits');
