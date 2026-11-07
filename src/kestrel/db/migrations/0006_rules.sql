-- 0006_rules.sql — strategies become editable data with a validation history

CREATE TABLE IF NOT EXISTS strategy_rule (
    code           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    note           TEXT,
    spec           TEXT NOT NULL,              -- JSON rule document
    enabled        INTEGER NOT NULL DEFAULT 1,
    builtin        INTEGER NOT NULL DEFAULT 0,
    last_verdict   TEXT,
    last_validated TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rule_validation (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    code      TEXT NOT NULL REFERENCES strategy_rule(code) ON DELETE CASCADE,
    verdict   TEXT NOT NULL,
    avg_exp   REAL NOT NULL DEFAULT 0,
    avg_edge  REAL NOT NULL DEFAULT 0,
    folds     TEXT NOT NULL DEFAULT '[]',
    symbols   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_validation_code ON rule_validation(code, id);
