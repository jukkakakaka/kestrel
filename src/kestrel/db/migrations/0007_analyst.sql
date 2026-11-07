-- 0007_analyst.sql — advice can come from rules or from a model, and says which

ALTER TABLE recommendation ADD COLUMN source TEXT NOT NULL DEFAULT 'rule';
ALTER TABLE recommendation ADD COLUMN ai_model TEXT;
ALTER TABLE recommendation ADD COLUMN ai_thesis TEXT;
ALTER TABLE recommendation ADD COLUMN ai_confidence INTEGER;
ALTER TABLE recommendation ADD COLUMN ai_rejected TEXT;

CREATE INDEX IF NOT EXISTS idx_rec_source ON recommendation(source, created_at);

-- what the model was shown, so any answer can be audited later
CREATE TABLE IF NOT EXISTS analyst_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    model       TEXT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    symbols     INTEGER NOT NULL DEFAULT 0,
    picks       INTEGER NOT NULL DEFAULT 0,
    accepted    INTEGER NOT NULL DEFAULT 0,
    rejected    TEXT NOT NULL DEFAULT '[]',
    brief       TEXT,
    raw_reply   TEXT,
    error       TEXT,
    duration_ms INTEGER
);
