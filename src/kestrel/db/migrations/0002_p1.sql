-- 0002_p1.sql — scanning, recommendations, notifications, receipts

CREATE TABLE IF NOT EXISTS strategy (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    weight      REAL NOT NULL DEFAULT 1.0,
    params      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scan_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    trigger     TEXT NOT NULL DEFAULT 'manual',   -- manual / schedule
    markets     TEXT NOT NULL DEFAULT '[]',       -- JSON array
    scanned     INTEGER NOT NULL DEFAULT 0,
    signals     INTEGER NOT NULL DEFAULT 0,
    plans       INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',  -- running / ok / failed
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_started ON scan_run(started_at);

CREATE TABLE IF NOT EXISTS signal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER NOT NULL REFERENCES scan_run(id) ON DELETE CASCADE,
    symbol_id   INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    strategy    TEXT NOT NULL,
    side        TEXT NOT NULL DEFAULT 'long',
    score       REAL NOT NULL,
    reasons     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signal_scan ON signal(scan_id);

CREATE TABLE IF NOT EXISTS recommendation (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id       INTEGER REFERENCES scan_run(id) ON DELETE SET NULL,
    symbol_id     INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    market        TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT NOT NULL,
    currency      TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    score         REAL NOT NULL,
    entry_low     INTEGER NOT NULL,
    entry_high    INTEGER NOT NULL,
    stop          INTEGER NOT NULL,
    target1       INTEGER NOT NULL,
    target2       INTEGER,
    qty           INTEGER NOT NULL,
    lot_size      INTEGER NOT NULL DEFAULT 1,
    risk_amount   INTEGER NOT NULL,
    est_fees      INTEGER NOT NULL DEFAULT 0,
    breakeven     INTEGER NOT NULL DEFAULT 0,
    r_multiple    REAL NOT NULL DEFAULT 0,
    confidence    REAL NOT NULL DEFAULT 0,
    valid_until   TEXT NOT NULL,
    invalidation  TEXT NOT NULL DEFAULT '[]',
    explain_zh    TEXT,
    explain_en    TEXT,
    status        TEXT NOT NULL DEFAULT 'active',  -- active / expired / withdrawn / executed / skipped
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rec_status ON recommendation(status, valid_until);
CREATE INDEX IF NOT EXISTS idx_rec_created ON recommendation(created_at);

CREATE TABLE IF NOT EXISTS notification (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_id      INTEGER NOT NULL REFERENCES recommendation(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,
    locale      TEXT NOT NULL DEFAULT 'zh-CN',
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / failed / skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at     TEXT,
    UNIQUE (rec_id, channel, locale)
);

CREATE TABLE IF NOT EXISTS receipt_token (
    code        TEXT PRIMARY KEY,
    rec_id      INTEGER NOT NULL REFERENCES recommendation(id) ON DELETE CASCADE,
    expires_at  TEXT NOT NULL,
    uses        INTEGER NOT NULL DEFAULT 0,
    max_uses    INTEGER NOT NULL DEFAULT 5,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS receipt (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_id        INTEGER NOT NULL REFERENCES recommendation(id) ON DELETE CASCADE,
    token_code    TEXT,
    action        TEXT NOT NULL,                   -- executed / partial / skipped
    fill_price    INTEGER,
    fill_qty      INTEGER,
    filled_at     TEXT,
    deviation_pct REAL,
    deviation_r   REAL,
    note          TEXT,
    source        TEXT NOT NULL DEFAULT 'link',    -- link / ntfy / manual
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_receipt_rec ON receipt(rec_id);

CREATE TABLE IF NOT EXISTS job_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    run_key     TEXT NOT NULL UNIQUE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    detail      TEXT
);

INSERT OR IGNORE INTO strategy (code, name, name_en, enabled, weight, params) VALUES
    ('trend_pullback', '趋势回踩', 'Trend pullback', 1, 1.0, '{}'),
    ('breakout_20',    '20 日突破', '20-day breakout', 1, 1.0, '{}'),
    ('mean_reversion', '超跌反弹', 'Mean reversion', 1, 0.8, '{}');
