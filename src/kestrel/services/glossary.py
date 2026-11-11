"""Glossary terms, seeded and kept current.

Terms are data so the user can suppress the ones they already know. The bar for
inclusion is "a beginner will meet this word in this app and not know it", and
every definition is written in plain language first, jargon second.
"""

from __future__ import annotations

from kestrel.db.engine import connect

TERMS: list[tuple[str, str, str, str, str]] = [
    # key, zh, en, body_zh, body_en
    (
        "entry_zone", "入场区间", "Entry zone",
        "建议下单的价格范围。不用非得等到最低点，在这个区间里成交都算按计划执行。",
        "The price band to buy in. Filling anywhere inside it counts as following the plan.",
    ),
    (
        "stop_loss", "止损", "Stop loss",
        "提前定好的认错价。跌破就卖，不是等它涨回来。止损的作用是让你亏得起下一次。",
        "A pre-set price where you admit you were wrong. It exists so one loss cannot end you.",
    ),
    (
        "target", "目标价", "Target",
        "计划获利了结的位置。到价先卖一半，剩下用移动止损跟，是最常见的做法。",
        "Where you plan to take profit. Selling half there and trailing the rest is the usual approach.",
    ),
    (
        "rr", "盈亏比", "Reward-to-risk",
        "赚的钱和亏的钱的比例。2:1 意味着冒着亏 1 块的风险去赚 2 块。低于 1.5:1 的生意不值得做——胜率再高也会被手续费磨掉。",
        "Profit divided by risk. 2:1 means risking 1 to make 2. Below 1.5:1 the maths stops working after fees.",
    ),
    (
        "position", "仓位", "Position size",
        "这笔交易投多少钱。由「能亏多少」倒推出来，不是凭感觉决定。",
        "How much money goes into one trade. Derived from the loss you can tolerate, not from conviction.",
    ),
    (
        "atr", "ATR（真实波幅）", "ATR",
        "这只股票每天平均波动多少钱。用来定止损距离：波动大就止损放宽，不然一点噪音就把你扫出去。",
        "The average daily price swing. Used to place stops far enough that noise does not take you out.",
    ),
    (
        "rsi", "RSI（相对强弱）", "RSI",
        "0 到 100 的热度计。70 以上算过热（追高危险），30 以下算过冷（可能在跌势里）。它只说明热度，不说明该买还是该卖。",
        "A 0–100 heat gauge. Above 70 is hot, below 30 is cold. It measures heat, not direction.",
    ),
    (
        "ma20", "20 日均线", "20-day average",
        "过去 20 天收盘价的平均值。价格在它上方通常代表短期偏强，跌破常被当作走弱信号。",
        "Average close of the last 20 sessions. Price above it is short-term strength.",
    ),
    (
        "ma60", "60 日均线", "60-day average",
        "中期趋势的分界线。价格在 60 日线下方时买进，等于在下跌趋势里接刀。",
        "The medium-term trend line. Buying below it is catching a falling knife.",
    ),
    (
        "pullback", "回踩", "Pullback",
        "涨上去之后往回跌一点，踩到均线附近再继续涨。比追高安全，因为止损距离更短。",
        "A small dip back to the average inside an uptrend. Safer than chasing, because the stop is closer.",
    ),
    (
        "breakout", "突破", "Breakout",
        "价格冲过前期高点。真突破通常伴随成交量放大；没量的突破经常是假动作。",
        "Price pushing past a prior high. Real breakouts come with volume.",
    ),
    (
        "volume", "成交量", "Volume",
        "当天成交了多少股。价格可以骗人，成交量很难——放量说明真有人在买卖。",
        "Shares traded today. Price can lie; volume usually cannot.",
    ),
    (
        "vol_ratio", "量比", "Volume ratio",
        "今天的成交量和最近平均水平比。大于 1.5 说明明显放量。",
        "Today's volume against the recent average. Above 1.5 is a clear expansion.",
    ),
    (
        "support", "支撑位", "Support",
        "跌到这里往往有人接盘、跌不太下去的价格。跌破支撑通常意味着要换一个更低的区间。",
        "A price where buyers tend to appear. Losing it usually means a lower range.",
    ),
    (
        "resistance", "阻力位", "Resistance",
        "涨到这里往往有人卖出、涨不太上去的价格。止盈常放在阻力位下方一点。",
        "A price where sellers tend to appear. Targets usually sit just below it.",
    ),
    (
        "gap", "跳空缺口", "Gap",
        "今天开盘直接跳过昨天的价格区间，中间没成交。缺口常被当作支撑或阻力，也可能被回补。",
        "A jump with no trading in between. Often acts as support or resistance, and may get filled.",
    ),
    (
        "drawdown", "回撤", "Drawdown",
        "从最高点跌下来的幅度。−20% 的回撤需要涨 25% 才回本，这就是回撤比涨幅更重要的原因。",
        "The fall from a peak. A 20% drawdown needs 25% to recover — that asymmetry is the point.",
    ),
    (
        "long", "做多", "Long",
        "先买入、后卖出，靠上涨赚钱。这个工具只会给你做多的建议。",
        "Buy first, sell later, profit from rising prices. This tool only suggests longs.",
    ),
    (
        "short", "做空", "Short",
        "先借来卖出、跌了再买回。亏损理论上无上限，新手不要碰。",
        "Borrow and sell first, buy back lower. Losses are theoretically unlimited — not for beginners.",
    ),
    (
        "limit_order", "限价单", "Limit order",
        "指定价格挂单，不到这个价不成交。缺点是有可能一直不成交。",
        "An order at a set price. The catch: it may never fill.",
    ),
    (
        "market_order", "市价单", "Market order",
        "立刻按当前最优价成交。一定能成交，但价格可能和你想的不一样。",
        "Fills immediately at the best available price. Certain to fill, uncertain at what price.",
    ),
    (
        "slippage", "滑点", "Slippage",
        "你想成交的价格和实际成交价的差。波动大的时候滑点会吃掉你一部分利润。",
        "The gap between the price you wanted and the price you got.",
    ),
    (
        "lot", "一手 / 整手", "Lot",
        "港股和 A 股必须按「手」买，一手可能是 100 股、500 股或 1000 股，每只票不同。美股可以只买 1 股。",
        "HK and A-shares trade in lots of 100/500/1000 depending on the stock. US allows single shares.",
    ),
    (
        "board_lot_cost", "整手成本", "Cost per lot",
        "一手的价格。1 万本金买不了一手贵价股，这是资金量的限制，不是策略问题。",
        "The price of one lot. A 10k account simply cannot buy an expensive stock's lot.",
    ),
    (
        "commission", "佣金", "Commission",
        "券商按成交额收的服务费。很多券商「免佣」，但平台费照收。",
        "The broker's cut. Many advertise zero commission while still charging platform fees.",
    ),
    (
        "platform_fee", "平台费", "Platform fee",
        "券商按笔或按股收的固定费用。它是小额定投最贵的部分——买 1000 港币也要付 15 块。",
        "A fixed per-order or per-share charge. It is what makes small orders expensive.",
    ),
    (
        "stamp_duty", "印花税", "Stamp duty",
        "香港政府收的交易税，买卖各收 0.1%。A 股只在卖出时收 0.05%。美股没有。",
        "HK charges 0.1% each side. A-shares charge 0.05% on sells only. The US has none.",
    ),
    (
        "breakeven", "保本价", "Break-even",
        "算上买卖两次手续费后，真正不亏钱的价格。它永远高于你的买入价。",
        "The price where you truly break even after both sides' fees. Always above your entry.",
    ),
    (
        "margin", "保证金 / 融资", "Margin",
        "拿持仓抵押向券商借钱。借钱要付利息，跌多了会被强制卖出。三层杠杆叠三层杠杆，等于自杀。",
        "Borrowing against your holdings. Costs interest and invites forced liquidation.",
    ),
    (
        "forced_liquidation", "强平", "Forced liquidation",
        "账户净值跌破维持保证金时，券商自动市价卖出你的持仓还债。不通知、不看理由。",
        "The broker sells your positions automatically when equity falls below maintenance margin.",
    ),
    (
        "ipo", "打新", "IPO subscription",
        "申购新上市的股票。现金认购不中签通常只损失手续费，中签可能赚也可能破发。",
        "Subscribing to a new listing. Cash applications cost only the fee if you miss.",
    ),
    (
        "allotment", "中签率", "Allotment rate",
        "申购后真正拿到股票的比例。热门股中签率常常低于 5%。",
        "The share of applications that actually get stock. Hot deals often allot under 5%.",
    ),
    (
        "dark_pool", "暗盘", "Grey market",
        "新股正式上市前一晚的场外交易。能提前卖出，但流动性差、价差大。",
        "Off-exchange trading the night before listing. Early exit, poor liquidity.",
    ),
    (
        "ex_dividend", "除权除息", "Ex-dividend",
        "分红或送股后，股价会相应下调。看到价格突然掉一截不一定是跌，可能只是除息。",
        "The price drops by the dividend on the ex-date. That gap is not a loss.",
    ),
    (
        "adjusted", "复权", "Adjusted prices",
        "把分红送股的影响还原后的价格。看长期走势必须用复权价，否则会看到假的暴跌。",
        "Prices corrected for dividends and splits. Always use adjusted data for long charts.",
    ),
    (
        "deadline", "建议截止时间", "Plan deadline",
        "这条建议的有效期。过了这个时间，价格条件已经变了，照着做等于刻舟求剑。",
        "When the plan goes stale. After this the price context has changed; don't act on it.",
    ),
    (
        "deviation", "执行偏差", "Execution deviation",
        "建议价和你实际成交价的差。长期看，这个偏差比选股准确率更决定你的收益。",
        "The gap between the suggested and the actual fill. Over time it matters more than stock picking.",
    ),
    (
        "signal_strength", "信号强度", "Signal strength",
        "策略打分，不是上涨概率。80% 只表示条件符合度高，不代表八成会涨。",
        "A rule score, not a probability. 80% means the setup matches well, not that it will rise.",
    ),
    # ---- 卖出与持仓管理：新手最容易在这里吃亏 ----
    (
        "trailing_stop", "移动止损", "Trailing stop",
        "赚钱之后把止损往上抬，让已经到手的利润不被吐回去。"
        "常见做法是涨到 1 倍风险时把止损提到买入价（保本），涨到 2 倍时提到赚 1 倍的位置。"
        "关键：止损只能往上抬，不能往下调——往下调等于取消止损。",
        "Raising the stop as the trade moves in your favour, so a winner cannot turn into "
        "a loser. The usual ladder: at +1R move to break-even, at +2R move to +1R. "
        "The rule is one-way: a stop may only move up.",
    ),
    (
        "take_profit", "分批止盈", "Scaling out",
        "不要一次全卖。到第一个目标先卖一半，把利润落袋；剩下的用移动止损跟，"
        "让它有机会跑得更远。一次全卖的问题是：你永远在猜顶。",
        "Selling in pieces instead of all at once. Take half at the first target, trail the "
        "rest. Selling everything at once means you are guessing the top.",
    ),
    (
        "time_stop", "时间止损", "Time stop",
        "买了之后很久没涨，说明当初的理由没成立。占着资金和注意力本身就是成本，"
        "所以到了这个周期的持仓上限还没走出来，就该换到更好的机会上。",
        "If a trade goes nowhere for longer than the horizon allows, the reason you bought "
        "it did not hold. Capital and attention have a cost; move on.",
    ),
    (
        "r_multiple", "R 倍数", "R multiple",
        "用「冒了多少钱的风险」当单位来衡量盈亏。风险 1 块赚了 3 块就是 +3R，亏光止损就是 −1R。"
        "好处是不管股票贵还是便宜、买多买少，所有交易都能放在一起比较。",
        "Profit measured in units of what you risked. Risk 1 to make 3 is +3R; a full stop-out "
        "is −1R. It makes trades of different sizes comparable.",
    ),
    (
        "risk_budget", "风险预算", "Risk budget",
        "这一笔最多允许亏多少钱。它先定，股数后算：预算 ÷ 每股止损距离 = 能买几股。"
        "顺序反过来（先想买多少股）是新手亏钱的主要原因。",
        "The most you allow yourself to lose on one trade. It is decided first; the share count "
        "is derived from it. Doing it the other way round is the classic beginner mistake.",
    ),
    (
        "stop_distance", "止损距离", "Stop distance",
        "买入价到止损价之间差多少。它同时决定两件事：这笔最多亏多少，以及能买多少股。"
        "止损太近会被日常波动扫掉，太远则同样的风险预算只能买很少。",
        "The gap between entry and stop. It sets both your loss per share and your position "
        "size. Too tight gets shaken out; too wide buys very little.",
    ),
    (
        "floating_pnl", "浮动盈亏", "Open P&L",
        "还拿着没卖时的账面盈亏。它随时会变，只有卖出才变成真的钱。"
        "所以「浮盈回撤」不算亏，但「止损被跌破还拿着」通常就是真的亏。",
        "The paper profit or loss on a position you still hold. It is not real until you sell.",
    ),
    (
        "exposure", "总仓位 / 敞口", "Exposure",
        "所有持仓加起来占本金的比例。三笔各 20% 的仓位不是三个独立决定，"
        "它们同涨同跌时就是一笔 60% 的重仓。",
        "The share of your capital currently at risk across all positions. Three 20% positions "
        "are not independent — they are one 60% bet when the market turns.",
    ),
    (
        "sector_cap", "行业集中度", "Sector concentration",
        "同一个行业最多同时持有几只。屏幕选出八只银行，那不是选股，是押注整个板块。",
        "How many names from one sector you allow at once. Eight banks from a screen is a "
        "sector bet wearing a stock picker's clothes.",
    ),
    (
        "fifo", "先进先出 / FIFO", "FIFO",
        "分批买入后卖出时，先平掉最早那一笔。这样每条建仓记录的价格和数量都不会被后来的操作改写，"
        "复盘时看到的历史才是真的。",
        "When you sell part of a position, the oldest lot is closed first. Each entry keeps its "
        "original price and size, so the history stays honest.",
    ),
    (
        "win_rate", "胜率", "Win rate",
        "赚钱的交易占全部交易的比例。它单独没有意义——胜率 90% 但每次赚 1 块亏 10 块，照样亏钱。"
        "要和盈亏比一起看。",
        "The share of trades that made money. Meaningless on its own: 90% wins that make 1 and "
        "lose 10 still loses money. Read it with reward-to-risk.",
    ),
    (
        "expectancy", "期望值", "Expectancy",
        "平均每一笔交易能赚（或亏）多少。它是唯一值得长期盯住的数字："
        "期望为正，做够多次就赚；期望为负，做越多亏越多。",
        "The average result per trade. It is the one number worth tracking: positive expectancy "
        "compounds, negative expectancy compounds the other way.",
    ),
    (
        "backtest", "回测", "Backtest",
        "把一套规则放到历史数据里重放，看看如果过去一直照着做会发生什么。"
        "它不能预测未来，但能告诉你这套规则是不是本来就不成立。",
        "Replaying a rule set over historical data to see what would have happened. It cannot "
        "predict the future, but it can tell you whether the rule was ever any good.",
    ),
    (
        "benchmark", "基准", "Benchmark",
        "用来对比的参照物。这里的基准是「同样的入场点、同样持有天数、不设止损」。"
        "跑不赢这个基准，说明你的规则只是运气好赶上了行情。",
        "The thing you compare against. Here it is the same entry held for the same number of "
        "days with no stop. Fail to beat that and your rule was just riding the market.",
    ),
    (
        "walk_forward", "滚动验证", "Walk-forward",
        "把历史切成几段，每段单独测试。只在某一段赚钱的规则通常是过拟合——"
        "它是照着那段历史编出来的，不是真的有效。",
        "Splitting history into segments and testing each separately. A rule that only works in "
        "one segment is usually fitted to that segment rather than actually working.",
    ),
    (
        "horizon", "交易周期", "Trading horizon",
        "你打算持有多久。日线看几天到几周，周线看几周到两个月，月线看几个月。"
        "周期决定止损该多宽、计划活多久、多久没涨就该放弃。",
        "How long you intend to hold. The horizon sets how wide a stop should be, how long a "
        "plan stays valid, and when a trade has overstayed.",
    ),
    (
        "candidate_pool", "候选池", "Candidate pool",
        "每次扫描会看的那批股票，按市值从各市场取。只扫自己的自选等于体检，不是选股。",
        "The names a scan is allowed to look at, taken by market cap per market. Scanning only "
        "your watchlist is a health check, not stock picking.",
    ),
    (
        "paper_trade", "模拟成交 / 模拟盘", "Paper trade",
        "用当前行情价假装成交，用来把「计划 → 成交 → 台账」这条链路跑通。"
        "它不连接券商，也不产生真实委托。",
        "Filling at the latest quote to exercise the plan → fill → journal path. It is not "
        "connected to a broker and places no real order.",
    ),
    (
        "limit_up_down", "涨跌停", "Price limit",
        "A 股每天最多涨跌 10%（部分板块 20%）。涨到上限叫涨停，这时通常买不到；"
        "跌到下限叫跌停，通常卖不掉。港股和美股没有这个限制。",
        "A-shares stop at ±10% a day (20% on some boards). At the limit you usually cannot buy "
        "or sell at all. Hong Kong and the US have no such limit.",
    ),
    (
        "t_plus_one", "T+1", "T+1 settlement",
        "A 股今天买的股票，明天才能卖。港股和美股是 T+0，当天就能卖。"
        "这意味着一笔错误当天没法纠正，只能靠止损位提前想清楚。",
        "A-shares bought today cannot be sold until tomorrow. Hong Kong and the US allow same-day "
        "exits. One more reason the stop has to be decided before you buy.",
    ),
    (
        "review", "复盘", "Post-trade review",
        "平仓之后回看：当初为什么买、后来发生了什么、有没有按计划做。"
        "复盘的价值不在结论，而在发现自己的重复错误。",
        "Looking back after you close: why you bought, what happened, whether you followed the "
        "plan. Its value is spotting the mistake you keep repeating.",
    ),
    (
        "order_type", "限价单 / 市价单", "Order types",
        "限价单是指定价格、不到价不成交；市价单是立刻成交、价格不确定。"
        "波动大的时候，市价单的成交价可能比你想的差很多。",
        "A limit order waits for your price; a market order fills now at whatever is available. "
        "In fast markets that can be far from what you expected.",
    ),
]


def seed() -> int:
    conn = connect()
    added = 0
    try:
        # keys that were renamed by later versions of this list
        conn.execute("DELETE FROM glossary WHERE key IN ('stop', 'expiry')")
        for key, zh, en, body_zh, body_en in TERMS:
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
