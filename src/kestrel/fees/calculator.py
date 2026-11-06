"""Fee engine: exchange rules plus broker plans, all in Decimal.

Numbers can be verified against a monthly statement with ``scripts/fee_verify.py``.
Anything the broker actually charges that is not modelled here must be added as an
exchange rule or a broker field — never hard-coded at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal


@dataclass(frozen=True, slots=True)
class BrokerPlan:
    code: str
    name: str
    market: str
    commission_rate: Decimal = Decimal("0")
    commission_min: Decimal = Decimal("0")
    platform_fixed: Decimal = Decimal("0")
    platform_per_share: Decimal = Decimal("0")
    platform_min: Decimal = Decimal("0")

    def commission(self, amount: Decimal) -> Decimal:
        fee = amount * self.commission_rate
        return max(fee, self.commission_min) if self.commission_rate or self.commission_min else Decimal("0")

    def platform(self, shares: Decimal) -> Decimal:
        fee = self.platform_fixed + shares * self.platform_per_share
        return max(fee, self.platform_min) if fee else Decimal("0")


PLANS: dict[str, BrokerPlan] = {
    # Hong Kong
    "fosun_hk": BrokerPlan("fosun_hk", "星财富（复星）", "HK", platform_fixed=Decimal("15")),
    "usmart_hk": BrokerPlan(
        "usmart_hk", "盈立香港", "HK",
        commission_rate=Decimal("0.0003"), platform_fixed=Decimal("12"),
    ),
    "za_hk": BrokerPlan(
        "za_hk", "众安银行", "HK",
        commission_rate=Decimal("0.0005"), commission_min=Decimal("18"),
    ),
    "futu_hk": BrokerPlan(
        "futu_hk", "富途", "HK",
        commission_rate=Decimal("0.0003"), platform_fixed=Decimal("15"),
    ),
    "tiger_hk": BrokerPlan(
        "tiger_hk", "老虎", "HK",
        commission_rate=Decimal("0.00029"), platform_fixed=Decimal("15"),
    ),
    # United States
    "fosun_us": BrokerPlan(
        "fosun_us", "星财富（复星）", "US",
        platform_per_share=Decimal("0.005"), platform_min=Decimal("1"),
    ),
    "usmart_us": BrokerPlan(
        "usmart_us", "盈立香港", "US",
        platform_per_share=Decimal("0.009"), platform_min=Decimal("1.88"),
    ),
    "za_us": BrokerPlan(
        "za_us", "众安银行", "US",
        platform_per_share=Decimal("0.0099"), platform_min=Decimal("1.99"),
    ),
    "ibkr_us": BrokerPlan(
        "ibkr_us", "IBKR", "US",
        platform_per_share=Decimal("0.005"), platform_min=Decimal("1"),
    ),
    # Mainland
    "cn_generic": BrokerPlan(
        "cn_generic", "通用 A 股", "CN",
        commission_rate=Decimal("0.00025"), commission_min=Decimal("5"),
    ),
}

DEFAULT_PLAN = {"HK": "fosun_hk", "US": "fosun_us", "CN": "cn_generic"}


@dataclass(slots=True)
class FeeBreakdown:
    commission: Decimal
    platform: Decimal
    stamp: Decimal
    exchange: Decimal
    settlement: Decimal
    total: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "commission": str(self.commission),
            "platform": str(self.platform),
            "stamp": str(self.stamp),
            "exchange": str(self.exchange),
            "settlement": str(self.settlement),
            "total": str(self.total),
        }


HK_SETTLE_MIN = Decimal("2")
HK_SETTLE_MAX = Decimal("100")


def hk_fees(plan: BrokerPlan, amount: Decimal, shares: Decimal, is_sell: bool) -> FeeBreakdown:
    commission = plan.commission(amount)
    platform = plan.platform(shares)
    stamp = (amount * Decimal("0.001")).quantize(Decimal("1"), rounding=ROUND_CEILING)
    trading = amount * Decimal("0.0000565")
    sfc = amount * Decimal("0.000027")
    frc = amount * Decimal("0.0000015")
    settle = min(max(amount * Decimal("0.00002"), HK_SETTLE_MIN), HK_SETTLE_MAX)
    exchange = trading + sfc + frc
    total = commission + platform + stamp + exchange + settle
    return FeeBreakdown(commission, platform, stamp, exchange, settle, total)


def us_fees(plan: BrokerPlan, amount: Decimal, shares: Decimal, is_sell: bool) -> FeeBreakdown:
    platform = plan.platform(shares)
    sec = max(amount * Decimal("0.0000206"), Decimal("0.01")) if is_sell else Decimal("0")
    taf = (
        min(max(shares * Decimal("0.000195"), Decimal("0.01")), Decimal("9.79"))
        if is_sell
        else Decimal("0")
    )
    exchange = sec + taf
    total = platform + exchange
    return FeeBreakdown(Decimal("0"), platform, Decimal("0"), exchange, Decimal("0"), total)


def cn_fees(plan: BrokerPlan, amount: Decimal, shares: Decimal, is_sell: bool) -> FeeBreakdown:
    commission = plan.commission(amount)
    stamp = (amount * Decimal("0.0005")) if is_sell else Decimal("0")
    transfer = amount * Decimal("0.00001")
    total = commission + stamp + transfer
    return FeeBreakdown(commission, Decimal("0"), stamp, transfer, Decimal("0"), total)


def quote_fees(
    market: str,
    amount: Decimal,
    shares: Decimal,
    is_sell: bool,
    broker: str | None = None,
) -> FeeBreakdown:
    code = broker or DEFAULT_PLAN.get(market, "cn_generic")
    plan = PLANS.get(code) or PLANS[DEFAULT_PLAN.get(market, "cn_generic")]
    market = market.upper()
    if market == "HK":
        return hk_fees(plan, amount, shares, is_sell)
    if market == "US":
        return us_fees(plan, amount, shares, is_sell)
    return cn_fees(plan, amount, shares, is_sell)


def round_trip(market: str, amount: Decimal, shares: Decimal, broker: str | None = None) -> Decimal:
    buy = quote_fees(market, amount, shares, False, broker).total
    sell = quote_fees(market, amount, shares, True, broker).total
    return buy + sell
