-- Autonomous Oil Trader — SQLite schema (WAL mode)
-- Apply: sqlite3 data/trader.db < db/schema.sql
--
-- WAL mode is enabled by the application on connect; it allows multiple
-- readers to coexist with one writer, which is what we need for the
-- daemon + dashboard + CLI all sharing the same DB file.
--
-- Conventions:
--   * INTEGER PRIMARY KEY uses SQLite's implicit autoincrement
--   * timestamps stored as TEXT in ISO 8601 format
--   * JSON columns stored as TEXT
--   * booleans stored as INTEGER 0/1

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- Cash and capital state
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cash (
  ts                TEXT NOT NULL,
  account           TEXT NOT NULL,
  starting_capital  REAL NOT NULL,
  current_balance   REAL NOT NULL,
  high_watermark    REAL NOT NULL,
  withdrawals       REAL NOT NULL DEFAULT 0,
  mode              TEXT NOT NULL CHECK (mode IN ('paper','draft','live','halt')),
  daily_pnl         REAL NOT NULL DEFAULT 0,
  daily_loss_halt   REAL NOT NULL,
  notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_cash_ts ON cash(ts);

-- ----------------------------------------------------------------------------
-- Strategy library state
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategies (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  class_path      TEXT NOT NULL,
  tier            TEXT NOT NULL CHECK (tier IN ('experimental','shadow','paper','draft','live','retired')),
  enabled         INTEGER NOT NULL DEFAULT 1,
  params          TEXT NOT NULL,
  ts_created      TEXT NOT NULL,
  ts_promoted     TEXT,
  promoted_by     TEXT,
  promoted_from   TEXT,
  notes           TEXT
);

CREATE TABLE IF NOT EXISTS strategy_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  strategy_id     TEXT NOT NULL,
  event           TEXT NOT NULL,
  from_tier       TEXT,
  to_tier         TEXT,
  reason          TEXT,
  detail          TEXT
);

-- ----------------------------------------------------------------------------
-- Recommendations
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT NOT NULL,
  source          TEXT NOT NULL,
  strategy_id     TEXT,
  thesis          TEXT NOT NULL,
  structure       TEXT NOT NULL,
  legs            TEXT NOT NULL,
  size_units      INTEGER NOT NULL,
  target_debit    REAL,
  max_loss        REAL NOT NULL,
  max_profit      REAL NOT NULL,
  expected_value  REAL,
  expiry_date     TEXT NOT NULL,
  confidence      REAL,
  status          TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired','executed')),
  approved_by     TEXT,
  approved_at     TEXT,
  rejection_reason TEXT,
  llm_model       TEXT,
  llm_tokens_in   INTEGER,
  llm_tokens_out  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendations(status);
CREATE INDEX IF NOT EXISTS idx_rec_ts     ON recommendations(ts);

-- ----------------------------------------------------------------------------
-- Orders
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id   INTEGER,
  ts_created          TEXT NOT NULL,
  ts_submitted        TEXT,
  ts_filled           TEXT,
  ib_order_id         INTEGER,
  combo_legs          TEXT NOT NULL,
  action              TEXT NOT NULL,
  qty                 INTEGER NOT NULL,
  limit_price         REAL NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('draft','submitted','partial','filled','cancelled','rejected')),
  fill_price          REAL,
  commission          REAL DEFAULT 0,
  mode                TEXT NOT NULL,
  walking_attempts    INTEGER DEFAULT 0,
  notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_rec    ON orders(recommendation_id);

-- ----------------------------------------------------------------------------
-- Positions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id    INTEGER,
  strategy_id          TEXT,
  ts_opened            TEXT NOT NULL,
  ts_closed            TEXT,
  structure            TEXT NOT NULL,
  legs                 TEXT NOT NULL,
  qty                  INTEGER NOT NULL,
  open_debit           REAL NOT NULL,
  close_credit         REAL,
  realized_pnl         REAL,
  status               TEXT NOT NULL CHECK (status IN ('open','closing','closed')),
  stop_loss_price      REAL,
  profit_target_price  REAL,
  exit_reason          TEXT,
  parent_position_id   INTEGER,
  mode                 TEXT NOT NULL,
  entry_atm_iv         REAL,          -- ATM IV at open (for vol-crush exit)
  entry_underlying     REAL           -- underlying price at open (for context)
);
CREATE INDEX IF NOT EXISTS idx_pos_status   ON positions(status);
CREATE INDEX IF NOT EXISTS idx_pos_strategy ON positions(strategy_id);

-- ----------------------------------------------------------------------------
-- Position marks
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position_marks (
  position_id     INTEGER NOT NULL,
  ts              TEXT NOT NULL,
  mark            REAL NOT NULL,
  unrealized_pnl  REAL NOT NULL,
  delta           REAL,
  gamma           REAL,
  vega            REAL,
  theta           REAL,
  underlying_last REAL,
  PRIMARY KEY (position_id, ts)
);

-- ----------------------------------------------------------------------------
-- News and observations
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT NOT NULL,
  ts_published      TEXT,
  source            TEXT NOT NULL,
  url               TEXT,
  headline          TEXT NOT NULL,
  body              TEXT,
  sentiment         REAL,
  impact            TEXT CHECK (impact IN ('low','medium','high','critical')),
  symbols_affected  TEXT,
  processed         INTEGER DEFAULT 0,
  hash              TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts);

CREATE TABLE IF NOT EXISTS user_observations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  text        TEXT NOT NULL,
  category    TEXT,
  weight      REAL DEFAULT 0.5,
  expires_at  TEXT NOT NULL,
  source_url  TEXT,
  used_in_decisions INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS patterns (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  name                TEXT NOT NULL,
  description         TEXT NOT NULL,
  category            TEXT,
  weight              REAL NOT NULL DEFAULT 0.5,
  active              INTEGER NOT NULL DEFAULT 1,
  ts_created          TEXT NOT NULL,
  times_referenced    INTEGER DEFAULT 0,
  notes               TEXT
);

-- ----------------------------------------------------------------------------
-- Cost ledger
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS costs (
  ts          TEXT NOT NULL,
  category    TEXT NOT NULL CHECK (category IN ('commission','llm','data','infra')),
  detail      TEXT,
  amount      REAL NOT NULL,
  context     TEXT,
  PRIMARY KEY (ts, category, detail, context)
);
CREATE INDEX IF NOT EXISTS idx_costs_ts ON costs(ts);

-- ----------------------------------------------------------------------------
-- Decision audit log
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT NOT NULL,
  agent             TEXT NOT NULL,
  prompt_hash       TEXT,
  inputs            TEXT,
  output            TEXT,
  action_taken      TEXT,
  llm_model         TEXT,
  llm_tokens_in     INTEGER,
  llm_tokens_out    INTEGER,
  llm_cost          REAL
);

-- ----------------------------------------------------------------------------
-- Findings (weekly review reports)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS findings (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT NOT NULL,
  period_start        TEXT NOT NULL,
  period_end          TEXT NOT NULL,
  report_md           TEXT NOT NULL,
  metrics             TEXT NOT NULL,
  proposed_changes    TEXT,
  approved_changes    TEXT,
  approved_by         TEXT,
  approved_at         TEXT
);

-- ----------------------------------------------------------------------------
-- Commentary stream
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commentary (
  ts            TEXT NOT NULL,
  level         TEXT NOT NULL CHECK (level IN ('info','warn','alert','critical')),
  topic         TEXT,
  message       TEXT NOT NULL,
  context       TEXT,
  PRIMARY KEY (ts, message)
);
CREATE INDEX IF NOT EXISTS idx_commentary_ts ON commentary(ts);

-- ----------------------------------------------------------------------------
-- Active rolling window of futures contracts
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS active_contracts (
  ts                TEXT NOT NULL,
  local_symbol      TEXT NOT NULL,
  symbol            TEXT NOT NULL,
  expiry            TEXT NOT NULL,
  con_id            INTEGER NOT NULL,
  state             TEXT NOT NULL CHECK (state IN ('tradeable','markable','retired')),
  dte_at_change     INTEGER,
  reason            TEXT,
  PRIMARY KEY (ts, local_symbol)
);
CREATE INDEX IF NOT EXISTS idx_active_state ON active_contracts(state);

CREATE TABLE IF NOT EXISTS roll_events (
  ts                TEXT NOT NULL PRIMARY KEY,
  added             TEXT,
  removed           TEXT,
  reason            TEXT,
  notes             TEXT
);

-- ----------------------------------------------------------------------------
-- Hourly (configurable) summaries — written by daemons/summarizer.py
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- Schema version
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL,
  notes      TEXT
);

INSERT OR IGNORE INTO schema_version (version, applied_at, notes)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'initial sqlite schema');
