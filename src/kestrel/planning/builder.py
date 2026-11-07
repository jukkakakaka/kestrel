"""Turn a strategy signal into an executable trade plan.

The position size is always computed locally from the risk budget. External agents
may adjust stop/targets/confidence, never the size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from kestrel.fees.calculator import round_trip
from kestrel.strategies.base import StrategyResult

FX = {"CNY": Decimal("1"), "HKD": Decimal("0.92"), "USD": Decimal("7.1")}
CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}
TZ = {"CN": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}

# Minimum reward-to-risk a plan must offer before it is worth showing.
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
    qty: int
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


def _round(value: Decimal, digits: str = "0.001") -> Decimal:
    return value.quantize(Decimal(digits))


def valid_until(market: str) -> str:
    """Plans expire at the close of the next tradable session in market time.

    Rolling forward past weekends matters more than it looks: a plan built on a
    Saturday used to expire at that Saturday's 16:00, so it died a few hours
    later without a single session in which the user could act on it.
    Holidays are not modelled - the worst case is one extra day of validity.
    """
    tz = ZoneInfo(TZ.get(market.upper(), "UTC"))
    now = datetime.now(tz)
    close = datetime.combine(now.date(), time(16, 0), tzinfo=tz)
    while close <= now or close.weekday() >= 5:  # Sat=5, Sun=6
        close = datetime.combine(close.date() + timedelta(days=1), time(16, 0), tzinfo=tz)
    return close.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")


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
) -> tuple[Plan | None, str | None]:
    """Build a plan, or explain why the setup is not worth taking.

    The reason matters: "one lot already exceeds the risk budget" is a capital
    problem the user can fix, while "reward too small" is the setup's fault.
    Treating them the same made the scan report look like it was broken.
    """
    last = Decimal(str(ind.get("last") or 0))
    atr = Decimal(str(ind.get("atr14") or 0))
    if last <= 0:
        return None, "no_price"

    currency = CURRENCY.get(market.upper(), "CNY")
    fx = FX.get(currency, Decimal("1"))
    capital_local = Decimal(str(capital_cny)) / fx

    entry_low = _round(last * Decimal("0.997"))
    entry_high = _round(last * Decimal("1.003"))

    # Stop: ATR based, tightened by the strategy hint when it is closer.
    atr_stop = last - atr * Decimal("2")
    hint = signal.stop_hint
    stop = atr_stop
    if hint:
        hint_d = Decimal(str(hint))
        if Decimal("0") < hint_d < last:
            stop = max(stop, hint_d) if hint_d > atr_stop else hint_d
    stop = min(stop, entry_low * Decimal("0.99"))

    risk_per_share = entry_low - stop
    if risk_per_share <= 0:
        return None, "bad_stop"
    stop_pct = risk_per_share / entry_low
    if stop_pct > Decimal("0.15"):
        return None, "stop_too_wide"
    if stop_pct < Decimal("0.015"):
        return None, "stop_too_tight"

    risk_budget = capital_local * Decimal(str(risk_pct)) / Decimal("100")
    raw_qty = risk_budget / risk_per_share
    lot = max(1, int(lot_size))
    qty = int(raw_qty // lot) * lot
    if qty <= 0:
        return None, "lot_too_big"

    # Never let one position exceed 35% of capital.
    cap_qty = int((capital_local * Decimal("0.35")) / entry_low // lot) * lot
    if cap_qty <= 0:
        return None, "lot_too_big"
    qty = min(qty, cap_qty)

    cost = entry_low * qty
    fees = round_trip(market, cost, Decimal(qty), broker)
    breakeven = entry_low + (fees / qty)

    target1 = entry_low + risk_per_share * Decimal("2")
    target2 = entry_low + risk_per_share * Decimal("3")
    if signal.target_hint:
        hint_t = Decimal(str(signal.target_hint))
        # A hint is only worth taking when it still pays for the risk. The 20-day
        # high sits just above a pullback entry more often than not, and using it
        # blindly produced plans with a 0.9:1 reward-to-risk.
        hint_r = (hint_t - entry_low) / risk_per_share if risk_per_share > 0 else Decimal("0")
        if hint_t > entry_high and hint_r >= MIN_R:
            target1 = hint_t
            target2 = entry_low + (hint_t - entry_low) * Decimal("1.5")

    r_multiple = float((target1 - entry_low) / risk_per_share)
    if r_multiple < 1.5:
        # Below 1.5:1 the fees and the odds both stop working. No plan beats a bad plan.
        return None, "low_reward"
    confidence = round(min(0.95, 0.35 + signal.score * 0.6), 3)

    plan = Plan(
        market=market.upper(),
        code=code,
        name=name,
        currency=currency,
        strategy=signal.code,
        score=round(signal.score, 3),
        entry_low=entry_low,
        entry_high=entry_high,
        stop=_round(stop),
        target1=_round(target1),
        target2=_round(target2),
        qty=qty,
        lot_size=lot,
        risk_amount=_round(risk_per_share * qty, "0.01"),
        est_fees=_round(fees, "0.01"),
        breakeven=_round(breakeven),
        r_multiple=round(r_multiple, 2),
        confidence=confidence,
        valid_until=valid_until(market),
        invalidation=["close_below_stop", "expired"],
        explain_zh=_explain_zh(market, name, signal, entry_low, stop, target1, qty, risk_pct),
        explain_en=_explain_en(market, name, signal, entry_low, stop, target1, qty, risk_pct),
    )
    return plan, None


def build(**kwargs) -> Plan | None:
    """Plan-only wrapper for callers that do not need the rejection reason."""
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


def _explain_zh(market, name, signal, entry, stop, target, qty, risk_pct) -> str:
    reasons = "；".join(REASON_ZH.get(r, r) for r in signal.reasons)
    risk = (entry - stop) / entry * 100
    return (
        f"为什么是它：{reasons}。\n"
        f"怎么买：在 {_fmt(entry)} 附近买入 {qty} 股。\n"
        f"错了怎么办：跌破 {_fmt(stop)} 就走，这一笔亏损控制在 {risk:.1f}% 左右，"
        f"约占总资金的 {risk_pct:.1f}%。\n"
        f"目标：先看 {_fmt(target)}，到了至少卖一半。"
    )


def _explain_en(market, name, signal, entry, stop, target, qty, risk_pct) -> str:
    reasons = ", ".join(signal.reasons)
    risk = (entry - stop) / entry * 100
    return (
        f"Why: {reasons}.\n"
        f"How: buy {qty} shares near {_fmt(entry)}.\n"
        f"If wrong: exit below {_fmt(stop)}, risking about {risk:.1f}% of the position "
        f"({risk_pct:.1f}% of capital).\n"
        f"Target: {_fmt(target)}; take at least half there."
    )
