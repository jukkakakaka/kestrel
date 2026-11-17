"""股数与交易数量规则。

涵盖三个市场的真实下单规则：
* A 股：普通主板按 100 股整手；科创板（688xxx）与北交所（8xxxxx/43xxxx/920xxx）
  买入 200 股起步、超出部分按 1 股递增；全市场实行 T+1。
* 港股：普通按个股每手股数（100/200/500/1000 等）买入；支持碎股的券商允许
  1 股起买，卖出碎股任何券商均可。
* 美股：支持 1 股，部分券商支持 0.0001 股小数股且有最低成交金额约束（fractional_min）。
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

#: 美股小数股最小步进单位
FRACTION_STEP = Decimal("0.0001")


def is_star_or_bse(market: str, code: str) -> bool:
    """是否为 A 股科创板（688）或北交所（8/43/920）标的。"""
    if str(market or "").upper() != "CN":
        return False
    c = str(code or "").strip()
    return c.startswith(("688", "8", "43", "920"))


def to_qty(value) -> Decimal:
    """解析用户或券商返回的股数，保留美股小数股精度。"""
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
    """向下取整到可交易最小步进单位（整手、1 股或 0.0001 股）。"""
    step = abs(Decimal(step)) or Decimal("1")
    return (Decimal(value) / step).to_integral_value(rounding=ROUND_DOWN) * step


def is_whole(value: Decimal) -> bool:
    return Decimal(value) == Decimal(value).to_integral_value()


def step_for(
    market: str,
    lot_size: int,
    *,
    odd_lot: bool = False,
    fractional: bool = False,
    code: str = "",
) -> Decimal:
    """返回指定市场、标的和券商能力下的数量递增步进。"""
    market = str(market).upper()
    if market == "US":
        return FRACTION_STEP if fractional else Decimal("1")
    if market == "HK":
        return Decimal("1") if odd_lot else Decimal(str(max(1, int(lot_size or 1))))
    if market == "CN":
        if is_star_or_bse(market, code):
            return Decimal("1")
        return Decimal(str(max(1, int(lot_size or 100))))
    return Decimal(str(max(1, int(lot_size or 1))))


def min_buy_qty(
    market: str,
    lot_size: int,
    *,
    odd_lot: bool = False,
    fractional: bool = False,
    code: str = "",
) -> Decimal:
    """返回该账户在该标的上买入开仓的最低合法数量。"""
    market = str(market).upper()
    if market == "CN":
        if is_star_or_bse(market, code):
            return Decimal("200")
        return Decimal(str(max(1, int(lot_size or 100))))
    if market == "HK":
        return Decimal("1") if odd_lot else Decimal(str(max(1, int(lot_size or 100))))
    if market == "US":
        return FRACTION_STEP if fractional else Decimal("1")
    return Decimal("1")


def display(value) -> str:
    """按人习惯的格式输出股数：100 显示 100，0.125 显示 0.125。"""
    amount = to_qty(value)
    return format(amount.normalize(), "f")


def broker_plan(broker: str | None, market: str | None = None):
    """按券商代码取出套餐（含碎股能力），数据库改过的值优先。"""
    try:
        from kestrel.fees import plans as plan_service

        if broker:
            loaded = plan_service.as_broker_plan(str(broker))
            if loaded is not None:
                return loaded
    except Exception:  # noqa: BLE001
        pass
    try:
        from kestrel.fees.calculator import DEFAULT_PLAN, PLANS

        if broker and str(broker) in PLANS:
            return PLANS[str(broker)]
        if market:
            return PLANS.get(DEFAULT_PLAN.get(market.upper(), ""))
        return PLANS.get(DEFAULT_PLAN.get("HK", ""))
    except Exception:  # noqa: BLE001
        return None


def tradable_step(
    market: str, lot_size: int, broker: str | None = None, code: str = ""
) -> Decimal:
    """计算账户在指定市场上的最小可买递增单位。"""
    plan = broker_plan(broker, market)
    odd_lot = bool(plan and plan.allows_odd_lot())
    fractional = bool(plan and plan.allows_fractional())
    return step_for(market, lot_size, odd_lot=odd_lot, fractional=fractional, code=code)


def tradable_buy_qty(
    raw_qty: Decimal,
    market: str,
    lot_size: int,
    broker: str | None = None,
    code: str = "",
    entry_price: Decimal | None = None,
) -> Decimal:
    """将理论风险股数向下规整为该市场与券商允许的合法买入股数，不满足最低门槛返回 0。"""
    plan = broker_plan(broker, market)
    odd_lot = bool(plan and plan.allows_odd_lot())
    fractional = bool(plan and plan.allows_fractional())
    step = step_for(market, lot_size, odd_lot=odd_lot, fractional=fractional, code=code)
    minimum = min_buy_qty(market, lot_size, odd_lot=odd_lot, fractional=fractional, code=code)
    qty = floor_step(raw_qty, step)
    if qty < minimum:
        return Decimal("0")
    # 美股小数股最低成交金额门槛校验（例如 $1）
    if (
        str(market).upper() == "US"
        and fractional
        and plan is not None
        and getattr(plan, "fractional_min", Decimal("0")) > 0
        and entry_price is not None
        and entry_price > 0
        and qty * entry_price < plan.fractional_min
    ):
        return Decimal("0")
    return qty


def validate_order_qty(
    market: str,
    code: str,
    qty: Decimal,
    lot_size: int,
    *,
    side: str = "buy",
    broker: str | None = None,
    held_qty: Decimal | None = None,
) -> tuple[bool, str]:
    """校验买卖数量是否符合交易所和券商规则。"""
    mkt = str(market or "").upper()
    qty = to_qty(qty)
    if qty <= 0:
        return False, "成交数量必须大于 0"

    if side == "sell":
        if mkt == "CN" and not is_whole(qty):
            return False, f"A 股只能按整股卖出，你填了 {display(qty)} 股"
        if mkt == "HK" and not is_whole(qty):
            return False, f"港股只能按整股卖出，你填了 {display(qty)} 股"
        if held_qty is not None and qty > held_qty:
            return False, f"卖出 {display(qty)} 股超过当前持仓 {display(held_qty)} 股"
        return True, ""

    # 买入规则
    lots = max(1, int(lot_size or (100 if mkt in ("CN", "HK") else 1)))
    if mkt == "CN":
        if not is_whole(qty):
            return False, f"A 股只能按整股买入，你填了 {display(qty)} 股"
        if is_star_or_bse(mkt, code):
            if qty < 200:
                return False, f"科创板/北交所 {code} 单笔买入至少 200 股（可 200 股以上按 1 股递增）"
            return True, ""
        if qty % lots:
            return False, f"{code} 每手 {lots} 股，A 股买入数量必须是 {lots} 的整数倍"
        return True, ""

    if mkt == "HK" and not is_whole(qty):
        return False, f"港股只能按整数股数成交，你填了 {display(qty)} 股"

    if mkt == "US":
        plan = broker_plan(broker, mkt)
        if not is_whole(qty) and not (plan and plan.allows_fractional()):
            return False, f"当前美股券商套餐不支持小数股，请填写整数股（你填了 {display(qty)}）"

    return True, ""


def db_value(value) -> int | float:
    """将数量转换为 sqlite3 可存储的整数或浮点。"""
    amount = to_qty(value)
    if is_whole(amount):
        return int(amount)
    return float(amount)
