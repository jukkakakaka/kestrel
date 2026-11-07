-- 0005_advice.sql — action labels, user decisions, scan history, pool columns

-- ---- universe (selection pool) ----
ALTER TABLE symbol ADD COLUMN is_pool INTEGER NOT NULL DEFAULT 0;
ALTER TABLE symbol ADD COLUMN pool_rank INTEGER;
ALTER TABLE symbol ADD COLUMN pool_cap INTEGER;

CREATE INDEX IF NOT EXISTS idx_symbol_pool ON symbol(is_pool, market, pool_rank);

-- ---- plans become advice with a decision ----
ALTER TABLE recommendation ADD COLUMN action TEXT NOT NULL DEFAULT 'wait';
ALTER TABLE recommendation ADD COLUMN headline TEXT;
ALTER TABLE recommendation ADD COLUMN learn_zh TEXT;
ALTER TABLE recommendation ADD COLUMN learn_en TEXT;
ALTER TABLE recommendation ADD COLUMN decided_at TEXT;
ALTER TABLE recommendation ADD COLUMN decided_note TEXT;
ALTER TABLE recommendation ADD COLUMN fill_price INTEGER;

CREATE INDEX IF NOT EXISTS idx_rec_decided ON recommendation(status, created_at);

-- ---- what each scan actually did ----
ALTER TABLE scan_run ADD COLUMN universe TEXT;
ALTER TABLE scan_run ADD COLUMN pool_size INTEGER;
ALTER TABLE scan_run ADD COLUMN skipped TEXT;
ALTER TABLE scan_run ADD COLUMN rejected TEXT;
ALTER TABLE scan_run ADD COLUMN duration_ms INTEGER;

-- ---- user editable broker fee plans ----
CREATE TABLE IF NOT EXISTS broker_plan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    market       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    commission   REAL NOT NULL DEFAULT 0,
    commission_min REAL NOT NULL DEFAULT 0,
    platform     REAL NOT NULL DEFAULT 0,
    platform_min REAL NOT NULL DEFAULT 0,
    platform_per_share REAL NOT NULL DEFAULT 0,
    per_order_min REAL NOT NULL DEFAULT 0,
    builtin      INTEGER NOT NULL DEFAULT 1,
    enabled      INTEGER NOT NULL DEFAULT 1,
    notes        TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- data source switches ----
CREATE TABLE IF NOT EXISTS data_source (
    key        TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 50,
    enabled    INTEGER NOT NULL DEFAULT 1,
    builtin    INTEGER NOT NULL DEFAULT 1,
    config     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---- notification channels, editable from the UI ----
CREATE TABLE IF NOT EXISTS notify_channel (
    key        TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 0,
    config     TEXT,
    last_test  TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
