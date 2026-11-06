-- 0001_init.sql — core schema for P0
-- Conventions: timestamps are UTC ISO-8601 text, money is integer micro-units.

CREATE TABLE IF NOT EXISTS market (
    code        TEXT PRIMARY KEY,              -- CN / HK / US
    name        TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    currency    TEXT NOT NULL,                 -- CNY / HKD / USD
    timezone    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS market_calendar (
    market      TEXT NOT NULL REFERENCES market(code) ON DELETE CASCADE,
    trade_date  TEXT NOT NULL,                 -- YYYY-MM-DD local
    is_open     INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    PRIMARY KEY (market, trade_date)
);

CREATE TABLE IF NOT EXISTS symbol (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT NOT NULL REFERENCES market(code),
    code        TEXT NOT NULL,                 -- 600519 / 00700 / NVDA
    name        TEXT NOT NULL,
    name_en     TEXT,
    exchange    TEXT,
    currency    TEXT NOT NULL,
    lot_size    INTEGER NOT NULL DEFAULT 1,
    sector      TEXT,
    board       TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (market, code)
);
CREATE INDEX IF NOT EXISTS idx_symbol_name ON symbol(market, name);

CREATE TABLE IF NOT EXISTS symbol_alias (
    alias       TEXT NOT NULL,
    symbol_id   INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    PRIMARY KEY (alias, symbol_id)
);

CREATE TABLE IF NOT EXISTS quote_latest (
    symbol_id   INTEGER PRIMARY KEY REFERENCES symbol(id) ON DELETE CASCADE,
    price       INTEGER NOT NULL,              -- micro-units
    prev_close  INTEGER,
    open        INTEGER,
    high        INTEGER,
    low         INTEGER,
    volume      INTEGER,
    turnover    INTEGER,
    change_pct  REAL,
    source      TEXT NOT NULL,
    quoted_at   TEXT NOT NULL,                 -- provider timestamp, UTC
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bar_daily (
    symbol_id   INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    trade_date  TEXT NOT NULL,
    open        INTEGER NOT NULL,
    high        INTEGER NOT NULL,
    low         INTEGER NOT NULL,
    close       INTEGER NOT NULL,
    volume      INTEGER,
    turnover    INTEGER,
    PRIMARY KEY (symbol_id, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_bar_date ON bar_daily(trade_date);

CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id   INTEGER NOT NULL REFERENCES symbol(id) ON DELETE CASCADE,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (symbol_id)
);

CREATE TABLE IF NOT EXISTS config_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,                 -- JSON encoded
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS data_source (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'builtin',  -- builtin / custom
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 100,
    config      TEXT,                          -- JSON
    last_ok_at  TEXT,
    last_error  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    actor       TEXT NOT NULL DEFAULT 'local',
    action      TEXT NOT NULL,
    target      TEXT,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);

-- seed markets
INSERT OR IGNORE INTO market (code, name, name_en, currency, timezone, enabled, sort_order)
VALUES
    ('CN', 'A 股',   'A-shares', 'CNY', 'Asia/Shanghai',   1, 1),
    ('HK', '港股',   'Hong Kong','HKD', 'Asia/Hong_Kong',  1, 2),
    ('US', '美股',   'US',       'USD', 'America/New_York',1, 3);

-- seed built-in data sources
INSERT OR IGNORE INTO data_source (code, name, kind, enabled, priority, config)
VALUES
    ('eastmoney', '东方财富', 'builtin', 1, 10, '{}'),
    ('tencent',   '腾讯行情', 'builtin', 1, 20, '{}'),
    ('sina',      '新浪行情', 'builtin', 1, 30, '{}'),
    ('yahoo',     'Yahoo',    'builtin', 1, 40, '{}');
