"""货币与精度辅助工具。

所有金额在 SQLite 中一律以微单位整数（1 单位 = 1e-6）存储，
防止浮点舍入误差；同时提供跨市场汇率换算与交易所最小变动价位（Tick Size）规整。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICRO = Decimal("1000000")

# 各市场对应结算币种与人民币基准汇率（统一唯一来源）
MARKET_CURRENCY: dict[str, str] = {"CN": "CNY", "HK": "HKD", "US": "USD"}
FX_TO_CNY: dict[str, Decimal] = {
    "CNY": Decimal("1"),
    "HKD": Decimal("0.92"),
    "USD": Decimal("7.1"),
}

_CN_ETF_PREFIXES = ("51", "56", "58", "15", "16")


def currency_for(market: str) -> str:
    return MARKET_CURRENCY.get(str(market or "").upper(), "CNY")


def to_cny(amount: Decimal | float | int, currency: str) -> Decimal:
    """将指定币种金额折算为人民币（CNY）。"""
    dec = amount if isinstance(amount, Decimal) else Decimal(str(amount or 0))
    rate = FX_TO_CNY.get(str(currency or "CNY").upper(), Decimal("1"))
    return dec * rate


def from_cny(amount_cny: Decimal | float | int, currency: str) -> Decimal:
    """将人民币金额折算为目标市场币种。"""
    dec = amount_cny if isinstance(amount_cny, Decimal) else Decimal(str(amount_cny or 0))
    rate = FX_TO_CNY.get(str(currency or "CNY").upper(), Decimal("1"))
    return dec / rate if rate else dec


def tick_size(price: Decimal, market: str, code: str = "") -> Decimal:
    """返回指定市场及标的价格所适用的最小报价变动单位（Tick Size）。"""
    mkt = str(market or "").upper()
    clean_code = str(code or "").strip()
    if mkt == "CN":
        if clean_code.isdigit() and len(clean_code) == 6 and clean_code.startswith(_CN_ETF_PREFIXES):
            return Decimal("0.001")
        return Decimal("0.01")
    if mkt == "US":
        return Decimal("0.01") if price >= Decimal("1") else Decimal("0.0001")
    if mkt == "HK":
        # 港交所标准证券价格价位表（Spread Table）
        if price <= Decimal("0.25"):
            return Decimal("0.001")
        if price <= Decimal("0.50"):
            return Decimal("0.005")
        if price <= Decimal("10"):
            return Decimal("0.01")
        if price <= Decimal("20"):
            return Decimal("0.02")
        if price <= Decimal("100"):
            return Decimal("0.05")
        if price <= Decimal("200"):
            return Decimal("0.10")
        if price <= Decimal("500"):
            return Decimal("0.20")
        if price <= Decimal("1000"):
            return Decimal("0.50")
        if price <= Decimal("2000"):
            return Decimal("1.00")
        if price <= Decimal("5000"):
            return Decimal("2.00")
        return Decimal("5.00")
    return Decimal("0.01")


def round_tick(price: Decimal | float | int | str, market: str, code: str = "") -> Decimal:
    """按交易所最小报价单位规整价格，保证建议价可直接用于挂限价单。"""
    dec = price if isinstance(price, Decimal) else Decimal(str(price or 0))
    if dec <= 0:
        return Decimal("0")
    step = tick_size(dec, market, code)
    rounded = (dec / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return max(step, rounded).normalize() if rounded == rounded.to_integral() else max(step, rounded)


def to_micro(value: Decimal | float | int | str) -> int:
    """将金额转换为整数微单位。"""
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((amount * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_micro(value: int | None) -> Decimal:
    """将整数微单位还原为 Decimal 金额。"""
    if value is None:
        return Decimal("0")
    return (Decimal(int(value)) / MICRO).quantize(Decimal("0.000001"))


def quantize(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def format_amount(value: int | Decimal | float, places: int = 2) -> str:
    """按千分位格式化显示金额。"""
    amount = from_micro(value) if isinstance(value, int) else Decimal(str(value))
    spec = f"{{:,.{places}f}}"
    return spec.format(amount)
