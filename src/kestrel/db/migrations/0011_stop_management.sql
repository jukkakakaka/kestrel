-- 0011_stop_management.sql — 移动止损要有历史
--
-- 止损从 380 抬到 400 不是一次数据修改，而是一个决策。
-- 记下「什么时候、从哪、到哪、为什么」，复盘时才知道当时是纪律还是心虚。

CREATE TABLE IF NOT EXISTS stop_change (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL REFERENCES trade(id) ON DELETE CASCADE,
    old_stop   INTEGER,
    new_stop   INTEGER NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_stop_change_trade ON stop_change(trade_id, id);

ALTER TABLE trade ADD COLUMN stop_updated_at TEXT;

-- 原始止损。止损被上移之后，「涨了几个 R」仍然必须按最初承担的风险算，
-- 否则移动止损会让 R 倍数越来越好看，却再也判断不了这笔交易做得好不好。
ALTER TABLE trade ADD COLUMN initial_stop INTEGER;

UPDATE trade SET initial_stop = stop_price WHERE initial_stop IS NULL;
