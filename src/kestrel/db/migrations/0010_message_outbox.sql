-- 0010_message_outbox.sql — 不挂在某条建议上的消息（持仓提醒、每日摘要）
--
-- notification 表要求 rec_id 非空，那是为「建议推送」设计的。持仓止损提醒
-- 不属于任何一条建议，所以单独一张表；重试与渠道状态沿用同样的字段语义。

CREATE TABLE IF NOT EXISTS message_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    url        TEXT,
    priority   TEXT NOT NULL DEFAULT 'default',
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending / sent / failed / skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_status ON message_outbox(status, id);
