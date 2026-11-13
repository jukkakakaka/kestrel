"""Share quantities.

Most of the world buys whole shares, but two cases are real and this app has
to get both right:

* **HK odd lots (碎股)** — less than one board lot, only some brokers let you
  open a position with them, and the fill is usually worse than the board lot;
* **US fractional shares (小数股)** — some brokers let you buy 0.0001 of a share
  with a small minimum notional.

Everything is a ``Decimal`` so a fractional fill does not get silently truncated
to zero. The storage columns are declared INTEGER, but SQLite's INTEGER affinity
stores a non-integral number as REAL without loss — the same behaviour as
NUMERIC — so no migration of existing rows is needed.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

#: Smallest fraction a US broker quotes for fractional orders.
FRACTION_STEP = Decimal("0.0001")


def to_qty(value) -> Decimal:
    """Parse a user/broker supplied quantity without losing fractions."""
    if value in (None, ""):
        return Decimal("0")
    try:
        amount = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
    if amount != amount.to_integral_value():
        amount = amount.quantize(FRACTION_STEP, rounding=ROUND_DOWN)
    return amount


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    """Round down to a tradable step (a lot, one share, or 0.0001)."""
    step = abs(Decimal(step)) or Decimal("1")
    return (Decimal(value) / step).to_integral_value(rounding=ROUND_DOWN) * step


def is_whole(value: Decimal) -> bool:
    return Decimal(value) == Decimal(value).to_integral_value()


def step_for(market: str, lot_size: int, *, odd_lot: bool = False,
             fractional: bool = False) -> Decimal:
    """Smallest tradable increment for this market and broker.

    ``CN`` never allows odd lots: buying 150 shares of a 100-share board lot is
    not a thing, whatever a broker claims.
    """
    market = str(market).upper()
    if market == "US" and fractional:
        return FRACTION_STEP
    if market == "HK" and odd_lot:
        return Decimal("1")
    if market == "US":
        return Decimal("1")
    return Decimal(str(max(1, int(lot_size or 1))))


def display(value) -> str:
    """Render a quantity the way a broker app does: 100, 0.125, not 100.0000."""
    amount = to_qty(value)
    text = format(amount.normalize(), "f")
    return text


def broker_plan(broker: str | None):
    """按券商代码取出套餐（含碎股能力）。数据库里改过的值优先。

    计划构建和回测都走这里，所以「券商能不能买碎股」只有一份判断。
    """
    try:
        from kestrel.fees import plans as plan_service

        if broker:
            loaded = plan_service.as_broker_plan(str(broker))
            if loaded is not None:
                return loaded
    except Exception:  # noqa: BLE001 - 取不到配置时退回纯手数逻辑
        pass
    try:
        from kestrel.fees.calculator import DEFAULT_PLAN, PLANS

        return PLANS.get(str(broker or "")) or PLANS.get(DEFAULT_PLAN.get("HK", ""))
    except Exception:  # noqa: BLE001
        return None


def tradable_step(market: str, lot_size: int, broker: str | None = None) -> Decimal:
    """这个账户在这个市场最小的可买单位。

    * A 股：固定整手（100 股），任何券商都一样；
    * 港股：券商支持碎股买入时是 1 股，否则是整手；
    * 美股：支持碎股时是 0.0001 股，否则 1 股。
    """
    plan = broker_plan(broker)
    odd_lot = bool(plan and plan.allows_odd_lot())
    fractional = bool(plan and plan.allows_fractional())
    return step_for(market, lot_size, odd_lot=odd_lot, fractional=fractional)


def db_value(value) -> int | float:
    """A quantity in a type sqlite3 accepts.

    整股存成 int（旧数据、旧查询、旧报表都按 int 读），小数股才落成 float。
    SQLite 的 INTEGER 亲和性会把非整数原样存成 REAL，两种值可以共存。
    """
    amount = to_qty(value)
    if is_whole(amount):
        return int(amount)
    return float(amount)
