-- Migration 001: initial schema baseline.
--
-- This is a no-op when applied to a freshly initialized DB (init-db already
-- ran schema.sql). It exists so the migration tracker has version 1 recorded
-- and subsequent migrations can be applied incrementally.
--
-- All migration files use CREATE/ALTER ... IF NOT EXISTS so they are safe to
-- re-run. tradectl migrate skips files whose version is already in schema_version.

INSERT OR IGNORE INTO schema_version (version, applied_at, notes)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'baseline schema (matches db/schema.sql)');
