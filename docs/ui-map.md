# UI map

| Route | Nav label | Purpose |
|---|---|---|
| `/` | 行情 | Live quotes, market open/closed state, links to the original source page |
| `/stock/{market}/{code}` | — | One symbol: 60-day chart, indicators, current plan, open position |
| `/advice` | 操作建议 | Today's plans as verbs (buy / watch), history, scan log |
| `/journal` | 交易台账 | Positions, closed trades, statement import |
| `/cost` | 成本计算 | Round-trip fees, break-even price, editable broker plans |
| `/sim` | 模拟盘 | Backtest a strategy on history |
| `/settings` | 设置 | Capital, markets, data sources, engine, alerts, backup, glossary |

Old paths redirect: `/scan` → `/advice`, `/fees` → `/cost`, `/backtest` → `/sim`.

## Why the names changed

- **看板 → 行情**: a dashboard implies decisions; this page only shows prices.
- **扫描 → 操作建议**: "scan" describes the machine's action. The user needs to know
  what to *do*, so the page is named after the output, and every card carries a verb.
- **回测 → 模拟盘**: backtesting is one technique; the page simulates a strategy over
  history, which is what a beginner actually wants to know.
