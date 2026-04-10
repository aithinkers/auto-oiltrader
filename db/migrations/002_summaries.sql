-- Migration 002: add summaries table for the hourly digest worker.
--
-- Safe to re-run: CREATE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS summaries (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT NOT NULL,
  period_start  TEXT NOT NULL,
  period_end    TEXT NOT NULL,
  headline      TEXT NOT NULL,
  body_md       TEXT NOT NULL,
  metrics_json  TEXT,
  pushed        INTEGER DEFAULT 0,
  push_target   TEXT
);
CREATE INDEX IF NOT EXISTS idx_summaries_ts ON summaries(ts);

INSERT OR IGNORE INTO schema_version (version, applied_at, notes)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'add summaries table');
