"""股票与交易术语词库服务（支持全局正文自动识别 + 悬停/点击实时查词）。

设计准则：
1. 彻底废除「卡片底下硬塞几个脱离语境的孤立词汇」旧设计。
2. 当用户开启学习开关时，前端会自动在页面正文、建议卡片、仓位表、弹窗与 AI 对话
   中自然出现这些词的位置加上柔和虚线下划线；鼠标悬停或点击即可弹出大白话解释、
   实战举例与「问 AI 助手」快捷入口。关闭开关后全站零下划线、零打扰。
"""

from __future__ import annotations

from kestrel.db.engine import connect
from kestrel.services import config as config_service

# 格式: (key, zh, en, body_zh, body_en, category, aliases_zh, aliases_en)
ENTRIES: list[tuple[str, str, str, str, str, str, tuple[str, ...], tuple[str, ...]]] = [
    # ---- 下单与执行 (order) ----
    (
        "entry_zone", "入场区间", "Entry zone",
        "建议挂单买入的价格范围。不用非得等最低点，落在这个区间里成交都算严格按计划执行。",
        "The suggested buy price range. Filling anywhere inside this band counts as following the plan.",
        "order",
        ("入场区间", "建议入场", "入场价", "买入区间"),
        ("Entry zone", "entry band", "entry price"),
    ),
    (
        "limit_order", "限价单", "Limit order",
        "自己定好最高买价（或最低卖价）再挂单，不到这个价格绝不成交，可有效避免开盘跳空追高。",
        "An order specifying the exact max buy or min sell price. Protects against chasing sharp gaps.",
        "order",
        ("限价单", "限价委托", "挂限价"),
        ("Limit order", "limit orders"),
    ),
    (
        "market_order", "市价单", "Market order",
        "按当前盘口最优价立刻成交。能保证买得到，但波动剧烈或流动性差时成交价可能明显偏离预期。",
        "Fills immediately at the best available quote. Guarantees a fill, not the price.",
        "order",
        ("市价单", "市价委托"),
        ("Market order", "market orders"),
    ),
    (
        "slippage", "滑点", "Slippage",
        "计划价格与实际成交均价之间的差额。频繁追高带来的负滑点会悄悄吞噬整套策略的正期望。",
        "The gap between expected fill price and actual fill price. Frequent chasing erodes expectancy.",
        "order",
        ("滑点", "成交滑点"),
        ("Slippage",),
    ),
    (
        "deviation", "执行偏差", "Execution deviation",
        "实际成交价与股数相对原计划偏离了多少（常用 % 和 R 倍数衡量）。长期看执行纪律比选股更决定收益。",
        "How far your actual fill price and size drifted from the plan, measured in % and R.",
        "order",
        ("执行偏差", "价格偏离", "执行率"),
        ("Execution deviation", "follow rate", "deviation"),
    ),
    (
        "fill_price", "成交均价", "Fill price",
        "多笔或部分成交加权平均后的真实成本价。台账会以它和实际股数为准计算盈亏与保本价。",
        "Weighted actual execution price. Your journal computes P&L and break-even from real fills, not plan quotes.",
        "order",
        ("成交均价", "实际成交价", "实际价"),
        ("Fill price", "actual fill"),
    ),
    (
        "partial_fill", "部分成交", "Partial fill",
        "只买到或卖出计划股数的一部分（例如计划买 200 股只成交了 100 股）。Kestrel 会如实记下实际成交部分。",
        "Only part of your planned shares filled. The journal logs the true quantity rather than pretending a full fill.",
        "order",
        ("部分成交", "分批买入", "分批卖出"),
        ("Partial fill", "partial fills"),
    ),
    (
        "deadline", "建议截止时间", "Plan deadline",
        "一条交易计划的有效时限。过期之后市场结构与价格位置已变化，再机械沿用旧价位属于刻舟求剑。",
        "When an advice plan expires. After this window, the setup assumptions are stale.",
        "order",
        ("建议截止时间", "有效期至", "计划过期"),
        ("Plan deadline", "valid until", "expired"),
    ),
    (
        "paper_trade", "模拟成交 / 模拟盘", "Paper trade",
        "用最新缓存行情价模拟登记一次成交，用于走通「计划 → 风控 → 成交 → 台账」链路，不向券商发单。",
        "Fills against the latest cached quote to exercise the journal pipeline without sending real broker orders.",
        "order",
        ("模拟成交", "模拟盘"),
        ("Paper trade", "paper trading"),
    ),
    (
        "statement_import", "对账单导入", "Statement import",
        "从券商导出的成交记录直接粘贴导入，系统自动识别代码、方向、数量与日期并按 FIFO 平仓核对。",
        "Paste broker trade statements directly to reconstruct FIFO entries, exits, and real fees.",
        "order",
        ("对账单导入", "对账单", "交割单"),
        ("Statement import", "statement"),
    ),
    (
        "order_intent", "委托意图", "Order intent",
        "准备向券商下发的一笔标准下单指令，先经过本地 Tick、手数、T+1 与资金预检再生成或执行。",
        "Structured pre-trade order ticket validated for tick size, lot rules, T+1, and cash before execution.",
        "order",
        ("委托意图", "准实盘"),
        ("Order intent", "pre-trade"),
    ),
    # ---- 仓位与风控 (risk) ----
    (
        "stop_loss", "止损", "Stop loss",
        "买之前就定好的最大认错价。一旦跌破就果断卖出，确保单笔看错永远不会伤筋动骨。",
        "A pre-planned exit price where your thesis is proven wrong, protecting your account from ruin.",
        "risk",
        ("止损价", "初始止损", "跌破止损", "止损"),
        ("Stop loss", "stop price", "initial stop"),
    ),
    (
        "trailing_stop", "移动止损", "Trailing stop",
        "盈利后单向把止损往上抬（例如 +1R 抬到保本、+2R 锁定 +1R 利润）。绝不能往下调——下移等于取消风控。",
        "Ratcheting the stop upward only as profits grow (+1R to break-even, +2R to +1R). Never move a stop down.",
        "risk",
        ("移动止损", "抬高止损", "保本止损"),
        ("Trailing stop", "move stop"),
    ),
    (
        "time_stop", "时间止损", "Time stop",
        "持有超过该周期的合理等待天数仍未走出预期收益（如 < +1R），主动离场把资金与注意力腾给更强的标的。",
        "Exiting a stagnant trade that has failed to reach +1R within the horizon's time budget.",
        "risk",
        ("时间止损", "持仓上限"),
        ("Time stop", "max hold"),
    ),
    (
        "target", "目标价", "Target price",
        "计划第一阶段兑现利润的价位。到达后常见做法是分批止盈卖出一部分，余仓用移动止损博取主升浪。",
        "Planned first profit-taking level. Common rule: scale out half and trail the rest.",
        "risk",
        ("目标价", "第一目标", "T1 目标"),
        ("Target price", "Target1", "target"),
    ),
    (
        "take_profit", "分批止盈", "Scaling out",
        "达到第一目标先兑现部分筹码锁定胜果，剩余仓位靠移动止损让利润奔跑，避免「一卖就飞」或「坐电梯回吐」。",
        "Selling part of a winner at target while trailing the remainder so winners can run.",
        "risk",
        ("分批止盈", "落袋为安"),
        ("Scaling out", "take partial", "take-profit"),
    ),
    (
        "breakeven", "保本价", "Break-even",
        "把买入和卖出双边手续费、印花税全算进去之后，真正做到不亏钱的卖出价，一定高于你的买入成交价。",
        "Your true zero-profit exit price after factoring in both buy-side and sell-side fees.",
        "risk",
        ("含费保本价", "保本价", "回本价"),
        ("Break-even", "breakeven"),
    ),
    (
        "rr", "盈亏比", "Reward-to-risk",
        "潜在盈利空间除以承担风险的距离（如 2.5:1 代表冒亏 1 元风险赚 2.5 元）。长期低于 1.5:1 很难覆盖交易成本。",
        "Potential reward divided by stop risk (e.g., 2.5:1). Below 1.5:1 rarely survives real fees over time.",
        "risk",
        ("净盈亏比", "盈亏比"),
        ("Reward-to-risk", "R:R"),
    ),
    (
        "r_multiple", "R 倍数", "R multiple",
        "用「初始承担的 1 份风险」作为度量单位的盈亏倍数：亏到止损记 -1R，赚到两倍风险记 +2R，方便不同价格股票横向比较。",
        "Profit or loss measured as a multiple of initial trade risk (-1R = full stop loss, +2R = 2x risk gained).",
        "risk",
        ("R 倍数", "R倍数"),
        ("R multiple", "R-multiple"),
    ),
    (
        "risk_budget", "风险预算", "Risk budget",
        "单笔交易最多愿意亏损的金额（例如总资金的 1%–2%）。先定能亏多少，再倒推买几股，是专业仓位管理的起点。",
        "Max currency loss allowed on a single trade (e.g., 1%–2% of equity), which determines share sizing.",
        "risk",
        ("风险预算", "单笔风险金", "风险金"),
        ("Risk budget", "risk amount", "risk per trade"),
    ),
    (
        "stop_distance", "止损距离", "Stop distance",
        "买入价与止损价之间的每股差距。止损距离越宽，在同等风险预算下能买的股数就必须相应减少。",
        "Price gap between entry and stop. Wider stops require smaller share counts for equal risk.",
        "risk",
        ("止损距离", "每股风险"),
        ("Stop distance",),
    ),
    (
        "position", "仓位", "Position size",
        "单只股票投入的资金量或占总资产比例。仓位必须由止损距离和风险预算倒算出来，绝不是拍脑袋押重注。",
        "Capital allocated to one holding, derived mathematically from risk budget divided by stop distance.",
        "risk",
        ("建议仓位", "单笔仓位", "仓位"),
        ("Position size", "sizing"),
    ),
    (
        "exposure", "总仓位 / 敞口", "Total exposure",
        "所有在持股票市值合计占账户总资产的百分比。熊市控制总敞口，是避开系统性大回撤的核心阀门。",
        "Combined market value of open holdings as a percentage of total equity.",
        "risk",
        ("总仓位", "仓位上限", "总敞口", "敞口"),
        ("Total exposure", "exposure", "exposure cap"),
    ),
    (
        "available_cash", "可用现金", "Available cash",
        "账户里当前未被持仓占用的流动资金。Kestrel 会随每次确认买入、卖出、手续费与手动校准实时同步更新。",
        "Unencumbered cash left in the account after buys, realized P&L, fees, and cash calibrations.",
        "risk",
        ("可用现金", "剩余现金", "现金余额"),
        ("Available cash", "cash balance"),
    ),
    (
        "total_equity", "总资产 / 净值", "Total equity",
        "可用现金加上所有持仓按当前最新市值折算的总和。它真实反映你从初始资金一路交易至今的净成果。",
        "Available cash plus live market value of all open positions converted to base currency.",
        "risk",
        ("账户总资产", "总资产", "总净值", "初始本金", "初始资金"),
        ("Total equity", "initial capital", "equity"),
    ),
    (
        "cash_calibration", "资金与持仓校准", "Account calibration",
        "当系统账本与真实券商 App 的可用余额或手里原有底仓不一致时，手动对账对齐或银证出入金的功能。",
        "Manual reconciliation of broker cash, deposits/withdrawals, or existing holdings.",
        "risk",
        ("手动校准", "资金校准", "补录底仓", "银证转入", "银证转出"),
        ("Calibrate", "calibration"),
    ),
    (
        "drawdown", "回撤 / 最大回撤", "Drawdown",
        "账户资金从前期高点向下的最大跌幅。亏损 20% 需要后续涨 25% 才能回本，因此控制回撤比追求暴利更重要。",
        "Peak-to-trough decline in equity. A 20% loss requires a 25% gain just to recover.",
        "risk",
        ("最大回撤", "回撤"),
        ("Drawdown", "max drawdown"),
    ),
    (
        "sector_cap", "行业集中度", "Sector cap",
        "同一行业板块最多允许同时持有的标的数上限，防止看似买了好几只票，其实全押在同一条行业风口上。",
        "Maximum simultaneous positions allowed in one industry sector to avoid hidden correlated bets.",
        "risk",
        ("行业集中度", "同行业上限"),
        ("Sector cap", "sector concentration"),
    ),
    (
        "rule_break", "破规交易", "Rule break",
        "没有遵守原定止损、提前瞎卖、盲目加仓或超额重仓等违反交易纪律的操作。复盘专门统计它的代价。",
        "A trade where you violated your own stop, sizing, or exit rules — tracked separately in review.",
        "risk",
        ("违背原计划", "破规交易", "破规"),
        ("Rule break", "rule-break"),
    ),
    (
        "margin", "保证金 / 融资", "Margin",
        "向券商借钱放大仓位。杠杆会同步放大滑点与心态波动，跌穿维持线还会被强制平仓。Kestrel 仅做无杠杆多头。",
        "Borrowing funds from a broker to amplify size. Increases risk of forced liquidation.",
        "risk",
        ("保证金", "融资融券"),
        ("Margin",),
    ),
    (
        "forced_liquidation", "强平", "Forced liquidation",
        "保证金账户净值跌破券商风控线时，系统不问理由直接市价抛售你的持仓还债。",
        "Involuntary market liquidation triggered when account equity falls below maintenance margin.",
        "risk",
        ("强平", "强制平仓"),
        ("Forced liquidation",),
    ),
    # ---- 技术指标与趋势结构 (indicator) ----
    (
        "atr", "ATR（真实波幅）", "ATR",
        "Average True Range，衡量股票近期日均真实波动幅度。用于科学设定「既不被正常杂波扫出、又不过宽」的止损距离。",
        "Average True Range — measures typical daily volatility used to size realistic ATR-based stops.",
        "indicator",
        ("ATR", "真实波幅", "ATR14"),
        ("ATR", "Average True Range"),
    ),
    (
        "rsi", "RSI（相对强弱指标）", "RSI",
        "0–100 的动量热度表（采用 Wilder 平滑算法）。>70 常见短线过热，45–65 偏健康上升，跌破 30 常伴随弱势抛压。",
        "Wilder 0–100 momentum gauge. Above 70 warns of short-term overbought conditions; 45–65 fits healthy pullbacks.",
        "indicator",
        ("RSI", "相对强弱", "RSI14"),
        ("RSI", "Wilder RSI"),
    ),
    (
        "macd", "MACD", "MACD",
        "由快慢指数均线差值及其信号线构成的趋势动量指标。柱状翻红或金叉多头常辅助确认动能回暖。",
        "Trend-following momentum indicator based on 12/26 EMAs and a 9-period signal line.",
        "indicator",
        ("MACD", "柱状图"),
        ("MACD", "MACD signal"),
    ),
    (
        "ma20", "20 日均线 (MA20)", "MA20",
        "最近 20 个周期收盘价的平均值，常代表短期波段生命线。强势股回踩 MA20 缩量企稳是常见低吸结构。",
        "20-period simple moving average — key short-term trend guide and pullback support line.",
        "indicator",
        ("20 日均线", "20日线", "MA20", "SMA20"),
        ("MA20", "20-day average", "SMA20"),
    ),
    (
        "ma60", "60 日均线 (MA60)", "MA60",
        "中期牛熊与大趋势分界线。价格站在稳步向上的 MA60 之上做多，胜率远高于在下跌均线下方抢反弹。",
        "60-period moving average — medium-term trend filter separating uptrends from downtrends.",
        "indicator",
        ("60 日均线", "60日线", "MA60", "SMA60"),
        ("MA60", "60-day average", "SMA60"),
    ),
    (
        "slope", "均线斜率", "MA slope",
        "均线近几个交易日是向上昂头还是向下拐头。只在 20 日均线斜率向上时做回踩，能躲开大批下跌中继。",
        "Rate of change of a moving average. Positive MA20 slope filters out failing bounces.",
        "indicator",
        ("均线斜率", "斜率"),
        ("slope", "MA slope"),
    ),
    (
        "pullback", "回踩", "Pullback",
        "上涨趋势中股价短暂回调至均线或前高支撑附近企稳，比追突破止损更近、盈亏比通常更好。",
        "A controlled dip toward moving-average support inside an active uptrend, offering tighter stops.",
        "indicator",
        ("回踩", "均线回踩", "回踩企稳"),
        ("Pullback",),
    ),
    (
        "breakout", "突破", "Breakout",
        "股价放量越过近期盘整平台高点，标志着原有阻力被买盘消化并可能开启一段新波段。",
        "Price clearing a well-defined resistance high, ideally confirmed by expanding volume.",
        "indicator",
        ("突破", "平台突破", "向上突破"),
        ("Breakout",),
    ),
    (
        "vcp", "波动收缩 (VCP)", "VCP",
        "Volatility Contraction Pattern：股价回调幅度逐步收窄、成交量同步萎缩，说明浮筹洗净，临近有效突破窗口。",
        "Volatility Contraction Pattern — tightening price swings on dry volume ahead of a potential breakout.",
        "indicator",
        ("VCP", "波动收缩", "缩量收敛"),
        ("VCP", "volatility contraction"),
    ),
    (
        "volume", "成交量", "Volume",
        "一段时间内实际换手的股数。价格可以被少量资金短暂拉高，但真突破必须得到成交量同步认可。",
        "Total shares traded during a bar. Confirms whether a price move has real institutional participation.",
        "indicator",
        ("成交量", "放量", "缩量"),
        ("Volume",),
    ),
    (
        "vol_ratio", "量比", "Volume ratio",
        "当天成交量（盘中按已开盘分钟数折算）与近期平均成交量的比值。>1.5 代表明显放量活跃，0.6 以下常指缩量蓄势。",
        "Session-normalized current volume divided by recent average volume. >1.5 indicates active expansion.",
        "indicator",
        ("量比", "盘中量比"),
        ("Volume ratio", "vol ratio"),
    ),
    (
        "support", "支撑位", "Support",
        "历史上买盘集中涌现、多次止跌企稳的价格带。跌破有效支撑通常意味着短期看多逻辑破坏。",
        "A price level where buying demand repeatedly halted declines.",
        "indicator",
        ("支撑位", "支撑"),
        ("Support",),
    ),
    (
        "resistance", "阻力位", "Resistance",
        "前期套牢筹码或抛压集中的高点区域。目标价通常提前落在强阻力区下方，避免硬扛回落。",
        "A price area where selling supply previously capped advances.",
        "indicator",
        ("阻力位", "压力位", "阻力"),
        ("Resistance",),
    ),
    (
        "gap", "跳空缺口", "Gap",
        "今日开盘价直接越过昨日最高或最低价形成的价格空白。大幅跳空突破或向下破位在回测与挂单中均需纳入滑点考量。",
        "An opening jump leaving no trades between yesterday's range and today's open.",
        "indicator",
        ("跳空缺口", "跳空", "缺口"),
        ("Gap",),
    ),
    (
        "adjusted", "复权", "Adjusted prices",
        "剔除分红、送转股造成的股价除权缺口后的连续历史价格，用于计算均线、ATR 与回测收益，避免把分红误判成暴跌。",
        "Historical prices adjusted for dividends and splits so indicators stay continuous.",
        "indicator",
        ("复权", "前复权", "除权除息"),
        ("Adjusted prices", "Ex-dividend"),
    ),
    (
        "regime", "牛熊环境 (Regime)", "Market regime",
        "对大盘指数（如沪深300 / 恒生指数 / 标普500）所处牛市、震荡或熊市状态的客观定量识别，用来动态决定总仓位与策略门槛。",
        "Quantitative market classification (bull / sideways / bear) driving position sizing and signal thresholds.",
        "indicator",
        ("牛熊环境", "市场环境", "Regime", "偏多环境", "震荡环境", "偏弱环境"),
        ("Market regime", "regime"),
    ),
    (
        "signal_strength", "信号强度", "Signal score",
        "规则条件的综合契合度得分，并非「一定涨」的概率保证。同一信号仍需依赖止损与盈亏比把关。",
        "Composite score of how cleanly a setup matches rule criteria — not a guarantee of profit.",
        "indicator",
        ("信号强度", "规则评分", "评分"),
        ("Signal score", "Signal strength"),
    ),
    # ---- 市场制度与费用 (market) ----
    (
        "lot", "整手 / 一手", "Board lot",
        "A 股主板（除科创/北交所外）与港股最小按整手交易的股数基数（如 100 股/手，汇控 400 股/手）；美股 1 股即可买卖。",
        "Minimum standard lot unit in CN/HK markets (e.g., 100 or 400 shares per lot); US allows single shares.",
        "market",
        ("整手", "一手", "每手股数"),
        ("Board lot", "Lot size", "lot"),
    ),
    (
        "star_bse_lot", "科创板/北交所 200+1", "STAR/BSE 200+1",
        "A 股科创板（688 开头）与北交所（8/43/920 开头）规定单笔买入申报至少 200 股，超出后可按 1 股递增。",
        "CN STAR Market (688) and BSE (8/43/920) require at least 200 shares per buy order, then 1-share steps.",
        "market",
        ("科创板", "北交所", "200 股"),
        ("STAR Market", "STAR/BSE"),
    ),
    (
        "odd_lot", "碎股", "Odd lot",
        "不足一整手的零散股票。港股部分券商支持 1 股起买碎股，所有券商均可卖出碎股；A 股除特定板块外只能卖出零碎余股、不能买入碎股。",
        "Shares fewer than one standard board lot. Sellable in HK/CN; some HK brokers allow buying odd lots.",
        "market",
        ("碎股",),
        ("Odd lot",),
    ),
    (
        "fractional", "小数股", "Fractional shares",
        "支持按 0.0001 股等小数份买入高价美股（如苹果、英伟达或伯克希尔），让小资金也能精确匹配止损预算。",
        "Buying less than one full US share (e.g., 0.25 shares), letting smaller accounts size positions accurately.",
        "market",
        ("小数股",),
        ("Fractional shares",),
    ),
    (
        "t_plus_one", "T+1 制度", "T+1 settlement",
        "A 股当天买入的股票，必须等到下一个交易日才能卖出；港股与美股实行 T+0 当天买卖。因此在 A 股更忌盘中冲动重仓。",
        "CN A-shares bought today cannot be sold until the next trading day, unlike T+0 in HK/US.",
        "market",
        ("T+1",),
        ("T+1",),
    ),
    (
        "t_plus_zero", "T+0 制度", "T+0 settlement",
        "港股与美股支持当日买入后同日卖出离场，遇到突发黑天鹅或假突破可即时止损纠错。",
        "HK and US stocks allow buying and selling on the same trading day.",
        "market",
        ("T+0",),
        ("T+0",),
    ),
    (
        "limit_up_down", "涨跌停板", "Price limit",
        "A 股日内涨跌幅限制（主板 ±10%，创业板/科创板 ±20%）。跌停板常导致挂单卖不出去，止损距离需留出余量。",
        "Daily ±10%/±20% price bands on CN A-shares; locked limit-downs can block stop execution.",
        "market",
        ("涨跌停", "涨停", "跌停"),
        ("Price limit",),
    ),
    (
        "tick_size", "最小价位 (Tick)", "Tick size",
        "交易所规定的股票报价最小跳动单位（如 A 股 0.01 元、ETF 0.001 元、港股按股价档位不同）。Kestrel 会自动按此规整限价单价位。",
        "Smallest legal price increment on an exchange (e.g., CN 0.01, HK tiered spread table).",
        "market",
        ("最小报价", "最小价位", "Tick"),
        ("Tick size",),
    ),
    (
        "board_lot_cost", "整手门槛 / 整手成本", "Cost per lot",
        "买一手最便宜的股票至少要花多少钱。港股腾讯/比亚迪一手常需数万港元，小资金强做港股整手极易买不起或仓位失衡。",
        "Capital needed for a single board lot. Small accounts may get priced out of high-ticket HK lots.",
        "market",
        ("整手成本", "一手门槛"),
        ("Cost per lot",),
    ),
    (
        "commission", "佣金", "Commission",
        "券商按成交金额比例收取的经纪费，常有最低一笔收费限制；很多互联网券商虽免佣但会收固定平台费。",
        "Brokerage fee based on trade value, often paired with a minimum per-order floor.",
        "market",
        ("佣金", "免佣"),
        ("Commission",),
    ),
    (
        "platform_fee", "平台费", "Platform fee",
        "券商按笔或按股固定收取的交易通道使用费（如 HK$15/笔），小资金频繁短线时它是拉高保本线的最大元凶。",
        "Fixed per-order or per-share broker charge that heavily penalizes frequent small trades.",
        "market",
        ("平台费", "交收费", "过户费"),
        ("Platform fee",),
    ),
    (
        "stamp_duty", "印花税", "Stamp duty",
        "政府征收的证券交易税：港股买卖双边各 0.1%，A 股仅卖出单边收 0.05%，美股免印花税。",
        "Government tax on stock transfers: 0.1% each side in HK, 0.05% sell-only in CN, none in US.",
        "market",
        ("印花税",),
        ("Stamp duty",),
    ),
    (
        "horizon", "交易周期 / 交易频率", "Trading horizon",
        "计划持股的时间跨度与扫描切换节奏：快进快出（数日）、短线（1–3 周）、中线（1–2 月）、长线（季度级），也可由系统智能推荐。",
        "Intended holding duration and scan cadence (fast, short, medium, long, or advisor-recommended auto).",
        "market",
        ("交易周期", "交易频率", "快进快出", "短线", "中线", "长线"),
        ("Trading horizon", "fast", "short", "medium", "long"),
    ),
    (
        "candidate_pool", "候选池", "Candidate pool",
        "每次扫盘自动拉取的大市值流动性标的池。全市场筛选能避免只在自己几只自选里打转的视野盲区。",
        "Broad liquid market universe screened on every scan beyond your manual watchlist.",
        "market",
        ("候选池",),
        ("Candidate pool",),
    ),
    (
        "cold_start", "冷启动智能推荐", "Cold-start advisor",
        "初次启用或尚未完成充分历史回测时，系统根据你的本金规模、各市场手续费门槛和近期大盘牛熊环境，主动推荐最适合起步的市场与频率。",
        "When starting out with no personal track record, Kestrel recommends markets and cadence from capital, fees, and regime.",
        "market",
        ("冷启动", "智能推荐"),
        ("Cold start", "advisor"),
    ),
    (
        "ipo", "打新", "IPO",
        "申购即将上市新股。Kestrel 单独维护打新账本记录中签与暗盘/首日卖出损益。",
        "Subscribing to new stock listings, tracked separately in the IPO ledger.",
        "market",
        ("打新", "中签率", "暗盘"),
        ("IPO", "allotment", "grey market"),
    ),
    # ---- 复盘与统计 (review) ----
    (
        "long", "做多", "Long",
        "先买后卖、依靠股票上涨获利。Kestrel 专注于稳健做多与风险可控。",
        "Buying shares expecting price appreciation. Kestrel strictly models unlevered long trades.",
        "review",
        ("做多",),
        ("Long",),
    ),
    (
        "short", "做空", "Short",
        "先借股票高卖、跌了再买回平仓，理论上亏损无上限且需要融资利息，新手强烈建议避开。",
        "Borrowing shares to sell first and buy back lower. Carries unlimited downside risk.",
        "review",
        ("做空",),
        ("Short",),
    ),
    (
        "floating_pnl", "浮动盈亏", "Unrealized P&L",
        "仍在持仓中未卖出的账面盈亏（已扣除建仓时付出的买入手续费）。只有真实卖出后才会转变为已实现盈亏。",
        "Open paper gain or loss on current holdings (net of entry fees), locked in only upon exit.",
        "review",
        ("浮动盈亏", "浮盈", "浮亏", "已实现盈亏"),
        ("Unrealized P&L", "Realized P&L", "Open P&L"),
    ),
    (
        "fifo", "先进先出 (FIFO)", "FIFO",
        "同一只股票多次买入后分批卖出时，优先平掉最早买入的那一笔，确保历史成本、持有天数与分段费率账目清清楚楚。",
        "Closing the oldest open lot first on partial sells so entry cost basis and fees remain exact.",
        "review",
        ("先进先出", "FIFO"),
        ("FIFO",),
    ),
    (
        "win_rate", "胜率", "Win rate",
        "盈利平仓笔数占总平仓笔数的百分比。切忌孤立追求高胜率——90% 胜率但一次扛单亏光前面九次照样亏损。",
        "Share of closed trades that ended profitably. Must always be read alongside reward-to-risk.",
        "review",
        ("胜率",),
        ("Win rate",),
    ),
    (
        "expectancy", "期望值", "Expectancy",
        "平均每笔交易能期待赚到或亏掉多少比例（胜率×平均盈利 − 败率×平均亏损）。期望值为正，长期坚持才能复利增长。",
        "Average return per trade across wins and losses. Positive expectancy drives long-term compounding.",
        "review",
        ("期望值", "正期望"),
        ("Expectancy",),
    ),
    (
        "backtest", "回测 / 模拟验证", "Backtest",
        "把选股与风控规则放到过去几年历史 K 线里逐日按真实止损、ATR 滑点与双边费率重放检验，先剔除没优势的策略。",
        "Replaying rules over historical OHLC bars with stop-outs, gaps, and round-trip fees before trusting them live.",
        "review",
        ("历史验证", "策略回测", "回测"),
        ("Backtest",),
    ),
    (
        "benchmark", "基准对比", "Benchmark",
        "衡量一条交易规则是否真有超额能力的参照组：同样买点不设风控随机拿几天。跑不赢基准的规则会被自动停用。",
        "Control baseline comparing a rule's return against unguarded holding from identical entries.",
        "review",
        ("基准", "跑赢基准"),
        ("Benchmark",),
    ),
    (
        "walk_forward", "滚动验证", "Walk-forward",
        "把历史数据按时间分段独立检验，防止这条规则只是凑巧死记硬背某一年单边行情（过拟合）。",
        "Validating rule performance across consecutive out-of-sample windows to catch overfitting.",
        "review",
        ("滚动验证", "过拟合"),
        ("Walk-forward",),
    ),
    (
        "what_if_attr", "What-if 执行归因", "Execution attribution",
        "把真实成交价与数量算出的净盈亏，和「如果严格按计划入场区间与建议股数成交」的理论盈亏做差额对比，看清习惯到底在帮赚钱还是拖后腿。",
        "Comparing your true closed P&L against strict plan entry/size to quantify execution drag in currency.",
        "review",
        ("盈亏归因", "What-if", "归因"),
        ("What-if", "Execution attribution"),
    ),
    (
        "review", "复盘", "Post-trade review",
        "每笔平仓后的回顾总结：建仓动机是否合理、有没有按止损/止盈执行、哪些亏损源于违背计划的坏习惯。",
        "Structured post-exit review separating market variance from execution mistakes.",
        "review",
        ("复盘", "交易日记"),
        ("Post-trade review", "Journal"),
    ),
]

# 向后兼容老代码只读取五元组 TERMS 的方式
TERMS: list[tuple[str, str, str, str, str]] = [
    (k, zh, en, body_zh, body_en) for k, zh, en, body_zh, body_en, *_ in ENTRIES
]

TERM_META: dict[str, dict] = {
    k: {
        "category": cat,
        "aliases_zh": list(az),
        "aliases_en": list(ae),
        "zh": zh,
        "en": en,
        "body_zh": bzh,
        "body_en": ben,
    }
    for k, zh, en, bzh, ben, cat, az, ae in ENTRIES
}

CATEGORIES: dict[str, tuple[str, str]] = {
    "order": ("下单执行", "Execution"),
    "risk": ("仓位风控", "Risk & Sizing"),
    "indicator": ("技术指标", "Indicators"),
    "market": ("市场与费用", "Markets & Fees"),
    "review": ("复盘统计", "Review & Edge"),
}


def seed() -> int:
    """将全量词条同步写入 SQLite glossary 表，保留用户已勾选的 suppressed 状态。"""
    conn = connect()
    added = 0
    try:
        conn.execute("DELETE FROM glossary WHERE key IN ('stop', 'expiry')")
        for key, zh, en, body_zh, body_en, *_ in ENTRIES:
            cur = conn.execute(
                """
                INSERT INTO glossary (key, term_zh, term_en, body_zh, body_en)
                VALUES (?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    term_zh=excluded.term_zh, term_en=excluded.term_en,
                    body_zh=excluded.body_zh, body_en=excluded.body_en
                """,
                (key, zh, en, body_zh, body_en),
            )
            added += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return added


def list_all(locale: str = "zh-CN") -> list[dict]:
    """供设置页术语库展示的完整词条列表（带分类名和禁用状态）。"""
    en = str(locale).lower().startswith("en")
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM glossary ORDER BY key").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        item = dict(r)
        meta = TERM_META.get(item["key"], {})
        cat = meta.get("category", "review")
        cat_pair = CATEGORIES.get(cat, ("其他", "Other"))
        item["category"] = cat
        item["category_label"] = cat_pair[1] if en else cat_pair[0]
        item["aliases"] = meta.get("aliases_en" if en else "aliases_zh", [])
        out.append(item)
    return out


def payload_for_ui(locale: str = "zh-CN", *, force: bool = False) -> dict:
    """为前端全局自动术语识别与悬停气泡生成按词长优先排序的词典载荷。"""
    zh = str(locale).lower().startswith("zh")
    conn = connect()
    try:
        gloss_on = bool(config_service.get(conn, "ui.glossary", True))
        if not gloss_on and not force:
            return {"ok": True, "enabled": False, "items": {}, "matchers": []}
        rows = conn.execute("SELECT * FROM glossary WHERE suppressed = 0").fetchall()
    finally:
        conn.close()

    items: dict[str, dict] = {}
    matchers: list[dict[str, str]] = []
    for r in rows:
        key = r["key"]
        meta = TERM_META.get(key, {})
        cat = meta.get("category", "review")
        cat_pair = CATEGORIES.get(cat, ("术语", "Term"))
        term = r["term_zh"] if zh else r["term_en"]
        body = r["body_zh"] if zh else r["body_en"]
        aliases = list(meta.get("aliases_zh" if zh else "aliases_en") or [term])
        if term not in aliases:
            aliases.insert(0, term)
        items[key] = {
            "term": term,
            "body": body,
            "category": cat,
            "category_label": cat_pair[0] if zh else cat_pair[1],
            "aliases": aliases,
        }
        for phrase in aliases:
            clean = str(phrase or "").strip()
            if len(clean) >= 2:
                matchers.append({"key": key, "phrase": clean})

    # 长词优先匹配（如先匹配「移动止损」「止损距离」，再匹配「止损」），防止嵌套截断。
    matchers.sort(key=lambda m: len(m["phrase"]), reverse=True)
    return {"ok": True, "enabled": True, "items": items, "matchers": matchers}
