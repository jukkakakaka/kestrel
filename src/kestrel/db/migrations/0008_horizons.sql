-- 0008_horizons.sql — the same rules on daily, weekly and monthly bars

-- Which bar size a plan was made on. Old rows were all daily swing, so the
-- default is exactly right for them.
ALTER TABLE recommendation ADD COLUMN horizon TEXT NOT NULL DEFAULT 'short';

ALTER TABLE scan_run ADD COLUMN horizon TEXT;

-- Backtest history without a bar size is ambiguous once weekly and monthly
-- runs coexist.
ALTER TABLE backtest_run ADD COLUMN timeframe TEXT NOT NULL DEFAULT 'short';

-- A verdict belongs to a bar size: losing on daily says nothing about weekly.
ALTER TABLE rule_validation ADD COLUMN timeframe TEXT NOT NULL DEFAULT 'short';

-- How deep the last fetch went. A fresh cache can still be too shallow: asking
-- for monthly bars needs years of dailies, and "not stale" must not mean "never
-- look further back".
ALTER TABLE symbol ADD COLUMN bars_depth INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS rule_timeframe_state (
    code         TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    avg_edge     REAL NOT NULL DEFAULT 0,
    symbols      INTEGER NOT NULL DEFAULT 0,
    validated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (code, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_rec_horizon ON recommendation(horizon, created_at);
