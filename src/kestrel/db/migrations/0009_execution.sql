-- 0009_execution.sql — 让建议和真实成交真正闭环
--
-- 之前回执只记了价格，没有记数量，也没有写进台账：用户点了"已买入"，
-- 持仓页却是空的。这一版把成交当成一等公民，并为将来的券商 API 留出接入点。

-- 成交来源：手工录入 / 回执链接 / 对账单导入 / 券商 API
ALTER TABLE trade ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';

-- 建仓时对应建议的快照。建议可能过期、被清理，但"当初说好怎么买"必须留得住，
-- 否则事后没法判断到底是计划错了还是执行错了。
ALTER TABLE trade ADD COLUMN plan_snapshot TEXT;

-- 成交时间由用户填写（券商回单时间），不一定等于录入时间。
ALTER TABLE trade ADD COLUMN filled_at TEXT;

CREATE INDEX IF NOT EXISTS idx_trade_rec ON trade(rec_id);

-- 持仓提醒的去重表：同一种提醒对同一笔持仓只发一次，避免每半小时轰炸。
CREATE TABLE IF NOT EXISTS position_alert (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id  INTEGER NOT NULL REFERENCES trade(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,          -- stop_hit / target_hit / near_stop / overdue
    price     INTEGER,
    sent_at   TEXT NOT NULL DEFAULT (datetime('now')),
    channel   TEXT,
    UNIQUE (trade_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_alert_kind ON position_alert(kind, sent_at);

-- 首次使用引导是否完成。缺省 0，让老用户也看到一次引导（可以跳过）。
INSERT OR IGNORE INTO config_kv (key, value) VALUES ('ui.onboarded', '0');
