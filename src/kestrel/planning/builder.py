"""将策略信号转换为可执行的含费交易计划。

仓位股数完全由本地风控预算和可用现金决定，任何外部大模型或助手只能提供观点，
绝不允许绕过此模块擅自篡改仓位大小或取消止损。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from kestrel.core.money import currency_for, from_cny, round_tick
from kestrel.core.quantity import display, tradable_buy_qty, tradable_step
from kestrel.fees.calculator import round_trip
from kestrel.strategies.base import StrategyResult

# 最低风险回报比门槛：低于 1.5:1 的计划在扣除成本与正常胜率后长期期望为负
MIN_R = Decimal("1.5")


@dataclass(slots=True)
class Plan:
    market: str
    code: str
    name: str
    currency: str
    strategy: str
    score: float
    entry_low: Decimal
    entry_high: Decimal
    stop: Decimal
    target1: Decimal
    target2: Decimal | None
    qty: Decimal | int
    lot_size: int
    risk_amount: Decimal
    est_fees: Decimal
    breakeven: Decimal
    r_multiple: float
    confidence: float
    valid_until: str
    invalidation: list[str] = field(default_factory=list)
    explain_zh: str = ""
    explain_en: str = ""
    sizing_breakdown: dict = field(default_factory=dict)


def _round(value: Decimal, digits: str = "0.001") -> Decimal:
    return value.quantize(Decimal(digits))


def valid_until(market: str, horizon: str = "short") -> str:
    """计算计划失效时间（UTC ISO），与交易周期同步缩放。"""
    from datetime import datetime, timedelta

    from kestrel.services import timeframe as tf
    from kestrel.services.calendar import next_session_close

    base = next_session_close(market)
    ttl = tf.get(horizon).ttl_days
    if not ttl:
        return base
    try:
        moment = datetime.fromisoformat(base.replace("Z", "+00:00"))
    except ValueError:
        return base
    return (moment + timedelta(days=ttl)).isoformat(timespec="seconds")


def build_ex(
    *,
    market: str,
    code: str,
    name: str,
    ind: dict,
    signal: StrategyResult,
    capital_cny: float,
    risk_pct: float,
    lot_size: int,
    broker: str | None,
    horizon: str = "short",
    available_cash_cny: float | None = None,
) -> tuple[Plan | None, str | None]:
    """生成完整计划或返回精准拒绝原因。"""
    last = Decimal(str(ind.get("last") or 0))
    atr = Decimal(str(ind.get("atr14") or 0))
    if last <= 0:
        return None, "no_price"

    mkt = market.upper()
    currency = currency_for(mkt)
    capital_local = from_cny(Decimal(str(capital_cny)), currency)

    # 入场区间随波动率适度自适应（0.3%~0.8%），避免次日正常开盘即脱离区间
    atr_pct_raw = ind.get("atr_pct")
    band = Decimal("0.003")
    if atr_pct_raw:
        band = min(
            Decimal("0.008"),
            max(Decimal("0.003"), Decimal(str(atr_pct_raw)) / Decimal("100") * Decimal("0.18")),
        )
    entry_low = round_tick(last * (Decimal("1") - band), mkt, code)
    entry_high = round_tick(last * (Decimal("1") + band), mkt, code)
    if entry_high < entry_low:
        entry_high = entry_low

    # 止损：以 2 倍 ATR 为基准，策略给出更近的止损参考位时优先采用
    atr_stop = last - atr * Decimal("2")
    stop_raw = atr_stop if atr > 0 else last * Decimal("0.97")
    hint = signal.stop_hint
    if hint:
        hint_d = Decimal(str(hint))
        if Decimal("0") < hint_d < last:
            stop_raw = max(stop_raw, hint_d)
    stop_raw = min(stop_raw, entry_low * Decimal("0.99"))
    stop = round_tick(stop_raw, mkt, code)
    if stop >= entry_low:
        stop = round_tick(entry_low * Decimal("0.985"), mkt, code)

    risk_per_share = entry_low - stop
    if risk_per_share <= 0:
        return None, "bad_stop"
    stop_pct = risk_per_share / entry_low

    from kestrel.services import timeframe as tf

    floor, ceiling = tf.get(horizon).stop_band
    if stop_pct > Decimal(str(ceiling)):
        return None, "stop_too_wide"
    if stop_pct < Decimal(str(floor)):
        return None, "stop_too_tight"

    risk_budget = capital_local * Decimal(str(risk_pct)) / Decimal("100")
    raw_qty = risk_budget / risk_per_share
    lot = max(1, int(lot_size))
    step = tradable_step(mkt, lot, broker, code=code)

    qty = tradable_buy_qty(raw_qty, mkt, lot, broker, code=code, entry_price=entry_low)
    if qty <= 0:
        return None, "lot_too_big"

    # 单票持仓上限 35%
    cap_qty = tradable_buy_qty(
        (capital_local * Decimal("0.35")) / entry_low,
        mkt,
        lot,
        broker,
        code=code,
        entry_price=entry_low,
    )
    if cap_qty <= 0:
        return None, "lot_too_big"
    cap_applied = qty > cap_qty
    qty = min(qty, cap_qty)

    # 可用现金硬约束（预留约 0.5% 手续费与小数波动冗余）
    cash_capped = False
    if available_cash_cny is not None:
        cash_local = from_cny(Decimal(str(max(0.0, available_cash_cny))), currency)
        cash_qty = tradable_buy_qty(
            (cash_local * Decimal("0.992")) / entry_low,
            mkt,
            lot,
            broker,
            code=code,
            entry_price=entry_low,
        )
        if cash_qty <= 0:
            return None, "cash_insufficient"
        if qty > cash_qty:
            qty = cash_qty
            cash_capped = True

    cost = entry_low * qty
    fees = round_trip(mkt, cost, Decimal(qty), broker)
    breakeven = round_tick(entry_low + (fees / qty), mkt, code)

    target1 = round_tick(entry_low + risk_per_share * Decimal("2"), mkt, code)
    target2 = round_tick(entry_low + risk_per_share * Decimal("3"), mkt, code)
    if signal.target_hint:
        hint_t = round_tick(Decimal(str(signal.target_hint)), mkt, code)
        hint_r = (hint_t - entry_low) / risk_per_share if risk_per_share > 0 else Decimal("0")
        if hint_t > entry_high and hint_r >= MIN_R:
            target1 = hint_t
            target2 = round_tick(entry_low + (hint_t - entry_low) * Decimal("1.5"), mkt, code)

    r_multiple = float((target1 - entry_low) / risk_per_share)
    if r_multiple < 1.5:
        return None, "low_reward"
    confidence = round(min(0.95, 0.35 + signal.score * 0.6), 3)

    breakdown = {
        "capital_cny": round(float(capital_cny), 2),
        "currency": currency,
        "capital_local": round(float(capital_local), 2),
        "risk_pct": float(risk_pct),
        "risk_budget_local": round(float(risk_budget), 2),
        "risk_per_share": round(float(risk_per_share), 4),
        "raw_qty": round(float(raw_qty), 4),
        "step": display(step),
        "cap_applied": cap_applied,
        "cash_capped": cash_capped,
        "final_qty": display(qty),
    }

    plan = Plan(
        market=mkt,
        code=code,
        name=name,
        currency=currency,
        strategy=signal.code,
        score=round(signal.score, 3),
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        target1=target1,
        target2=target2,
        qty=qty,
        lot_size=lot,
        risk_amount=_round(risk_per_share * qty, "0.01"),
        est_fees=_round(fees, "0.01"),
        breakeven=breakeven,
        r_multiple=round(r_multiple, 2),
        confidence=confidence,
        valid_until=valid_until(mkt, horizon),
        invalidation=["close_below_stop", "expired"],
        explain_zh=_explain_zh(mkt, name, signal, entry_low, stop, target1, qty, risk_pct),
        explain_en=_explain_en(mkt, name, signal, entry_low, stop, target1, qty, risk_pct),
        sizing_breakdown=breakdown,
    )
    return plan, None


def build(**kwargs) -> Plan | None:
    """只返回 Plan 的简化封装。"""
    plan, _reason = build_ex(**kwargs)
    return plan


REASON_ZH = {
    "trend_up": "20 日线在 60 日线上方，且价格站在 60 日线上，趋势向上",
    "pullback_to_ma20": "价格回踩到 20 日线附近，是趋势中的低吸位",
    "rsi_neutral": "RSI 在中间区间，既没超买也没恐慌",
    "breakout_20d": "收盘价创 20 日新高，突破前期压力",
    "volume_expansion": "成交量明显放大，突破有资金配合",
    "rsi_oversold": "RSI 进入超卖区，短期跌得过头",
    "above_sma60": "仍在 60 日线上方，不是下跌趋势中的接刀",
}


def _fmt(value: Decimal, places: int = 3) -> str:
    return f"{value:,.{places}f}"


def _fmt_shares(qty: Decimal) -> str:
    return display(qty)


def _explain_zh(market, name, signal, entry, stop, target, qty, risk_pct) -> str:
    reasons = "；".join(REASON_ZH.get(r, r) for r in signal.reasons)
    risk = (entry - stop) / entry * 100
    return (
        f"为什么是它：{reasons}。\n"
        f"怎么买：挂限价单在 {_fmt(entry)} 附近买入 {_fmt_shares(qty)} 股。\n"
        f"错了怎么办：跌破 {_fmt(stop)} 就走，这一笔亏损控制在 {risk:.1f}% 左右，"
        f"约占总资金的 {risk_pct:.1f}%。\n"
        f"目标：先看 {_fmt(target)}，到了按规则分批止盈。"
    )


def _explain_en(market, name, signal, entry, stop, target, qty, risk_pct) -> str:
    reasons = ", ".join(signal.reasons)
    risk = (entry - stop) / entry * 100
    return (
        f"Why: {reasons}.\n"
        f"How: place a limit order for {_fmt_shares(qty)} shares near {_fmt(entry)}.\n"
        f"If wrong: exit below {_fmt(stop)}, risking about {risk:.1f}% of the position "
        f"({risk_pct:.1f}% of capital).\n"
        f"Target: {_fmt(target)}; scale out systematically there."
    )
