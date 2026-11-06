-- 0003_p2.sql — journal, IPO ledger, glossary, paper trading, backtest

CREATE TABLE IF NOT EXISTS trade (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT NOT NULL,
    code        TEXT NOT NULL,
    name        TEXT,
    currency    TEXT NOT NULL DEFAULT 'HKD',
    side        TEXT NOT NULL DEFAULT 'long',
    qty         INTEGER NOT NULL,
    entry_price INTEGER NOT NULL,
    exit_price  INTEGER,
    fees        INTEGER NOT NULL DEFAULT 0,
    stop_price  INTEGER,
    target_price INTEGER,
    opened_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT,
    status      TEXT NOT NULL DEFAULT 'open',      -- open / closed
    strategy    TEXT,
    rec_id      INTEGER REFERENCES recommendation(id) ON DELETE SET NULL,
    review      TEXT,
    rule_break  INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trade_status ON trade(status, market);

CREATE TABLE IF NOT EXISTS ipo_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    code        TEXT,
    lot_size    INTEGER NOT NULL,
    offer_price INTEGER NOT NULL,
    lots        INTEGER NOT NULL DEFAULT 1,
    fee         INTEGER NOT NULL DEFAULT 0,
    allot_qty   INTEGER NOT NULL DEFAULT 0,
    sell_price  INTEGER,
    listed_on   TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS glossary (
    key         TEXT PRIMARY KEY,
    term_zh     TEXT NOT NULL,
    term_en     TEXT NOT NULL,
    body_zh     TEXT NOT NULL,
    body_en     TEXT NOT NULL,
    suppressed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_id      INTEGER REFERENCES recommendation(id) ON DELETE CASCADE,
    market      TEXT NOT NULL,
    code        TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    fill_price  INTEGER NOT NULL,
    filled_at   TEXT NOT NULL DEFAULT (datetime('now')),
    exit_price  INTEGER,
    exited_at   TEXT,
    status      TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS backtest_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market      TEXT NOT NULL,
    code        TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    start_date  TEXT,
    end_date    TEXT,
    trades      INTEGER NOT NULL DEFAULT 0,
    win_rate    REAL,
    avg_r       REAL,
    max_dd      REAL,
    equity      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO glossary (key, term_zh, term_en, body_zh, body_en) VALUES
 ('atr', 'ATR（真实波幅）', 'ATR', '衡量这只股票平时一天波动多大。止损就是按它来定的：波动大的票，止损要给得宽一点，否则会被日常噪音扫掉。', 'Average True Range: how much the stock usually moves in a day. Stops are sized from it, so volatile names need wider stops or they get shaken out by noise.'),
 ('rsi', 'RSI（相对强弱）', 'RSI', '0 到 100 的指标。低于 30 通常说明短期跌过头了，高于 70 说明涨得有点热。单独看没用，要配合趋势。', 'A 0-100 momentum gauge. Below 30 usually means short-term oversold, above 70 overheated. Useless on its own — always read it with the trend.'),
 ('stop', '止损', 'Stop loss', '你提前定好的认错价。跌破就走，不等反弹。止损的作用不是让你不亏，是让你亏得可控。', 'The price where you admit you were wrong. Exit there, do not wait for a bounce. A stop does not prevent losses, it caps them.'),
 ('rr', '盈亏比 / R 倍数', 'R multiple', '赚的钱除以亏的钱。2:1 意味着你亏 1 块时，赚的是 2 块。盈亏比 2:1 时，胜率只要超过 34% 就不亏。', 'Potential gain divided by risk. At 2:1 you break even with a 34% win rate.'),
 ('position', '仓位', 'Position size', '这一笔买多少股。它不是拍脑袋定的，是用「能亏多少」倒推出来的：先定风险预算，再除以止损距离。', 'How many shares. It is derived, not guessed: risk budget divided by stop distance.'),
 ('breakeven', '保本价', 'Break-even', '算上手续费之后，涨到这个价你才真正不亏。港股的印花税和征费会让这个价明显高于你的买入价。', 'The price where you actually break even after fees. HK stamp duty and levies push it noticeably above your entry.'),
 ('pullback', '回踩', 'Pullback', '上涨趋势里价格短期回到均线附近。比追高安全，因为你的止损距离更短。', 'A short-term dip back to a moving average inside an uptrend. Safer than chasing, because your stop is closer.'),
 ('breakout', '突破', 'Breakout', '价格越过前面的压力位。真突破通常伴随放量，假突破往往缩量。', 'Price clearing a prior resistance. Real breakouts usually come with volume; fake ones often do not.'),
 ('ma20', '20 日均线', '20-day MA', '过去 20 天收盘价的平均。常被当作短期趋势的中枢，价格在它上方通常偏强。', 'Average close over 20 days. A common short-term trend anchor: above it is usually stronger.'),
 ('expiry', '建议有效期', 'Plan expiry', '这条建议到这个时间就作废。市场每天都在变，昨天的计划今天不一定还成立。', 'The plan is void after this time. Markets move; yesterday’s plan may not hold today.'),
 ('deviation', '执行偏差', 'Deviation', '你的真实成交价和建议入场价的差距。它衡量的是「你执行得准不准」，不是「股票好不好」。', 'The gap between your fill and the suggested entry. It measures your execution, not the stock.');
