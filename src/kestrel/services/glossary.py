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
