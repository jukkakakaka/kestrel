-- 0013_v040_agent_portfolio.sql — 动态资金账本、Agent 对话历史、准实盘委托表与复盘归因字段

-- 1. 资金变动与手动校准流水（支持出入金、可用现金对齐、底仓校准对冲）
CREATE TABLE IF NOT EXISTS cash_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,               -- deposit / withdraw / calibration / position_sync
    amount_micro INTEGER NOT NULL,            -- 正数增加现金，负数减少现金（人民币微单位）
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cash_ledger_at ON cash_ledger(created_at, id);

-- 2. 右侧炒股 Agent 助手对话历史持久化
CREATE TABLE IF NOT EXISTS copilot_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT 'default',
    role       TEXT NOT NULL,                 -- user / assistant / system
    content    TEXT NOT NULL,
    page_ctx   TEXT,
    rejected   INTEGER NOT NULL DEFAULT 0,    -- 是否因超出炒股/本产品话题范围被拒
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_copilot_session ON copilot_message(session_id, id);

-- 3. 准实盘委托意图与状态机（待接券商交易 API 时无缝升级为真实委托单）
CREATE TABLE IF NOT EXISTS order_intent (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    rec_id          INTEGER REFERENCES recommendation(id) ON DELETE SET NULL,
    trade_id        INTEGER REFERENCES trade(id) ON DELETE SET NULL,
    market          TEXT NOT NULL,
    code            TEXT NOT NULL,
    side            TEXT NOT NULL,            -- buy / sell
    order_type      TEXT NOT NULL DEFAULT 'LIMIT',
    qty             REAL NOT NULL,
    limit_price     INTEGER,
    stop_price      INTEGER,
    status          TEXT NOT NULL DEFAULT 'PENDING_SUBMIT',
    broker          TEXT,
    reject_reason   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_order_intent_status ON order_intent(status, created_at);

-- 4. 建议累计实际成交股数（支持分批建仓跟踪与部分成交进度展示）
ALTER TABLE recommendation ADD COLUMN fill_qty REAL;

-- 5. 交易复盘标签与初始买入手续费（支持分批平仓精确按比例分摊买入成本）
ALTER TABLE trade ADD COLUMN exit_reason TEXT;
ALTER TABLE trade ADD COLUMN entry_fees INTEGER;

UPDATE trade SET entry_fees = fees WHERE status = 'open' AND entry_fees IS NULL;

-- 6. 修复旧库从 0012 迁移后内置券商碎股能力未自动补齐的问题
UPDATE broker_plan SET odd_lot_buy = 1
 WHERE builtin = 1 AND key IN ('fosun_hk', 'futu_hk');

UPDATE broker_plan SET fractional_us = 1, fractional_min = 1
 WHERE builtin = 1 AND key IN ('fosun_us', 'usmart_us', 'ibkr_us');
