"""Fee engine golden cases — these numbers should match a broker statement."""

from __future__ import annotations

from decimal import Decimal

from kestrel.fees.calculator import PLANS, quote_fees, round_trip


def test_hk_stamp_duty_rounds_up():
    fees = quote_fees("HK", Decimal("10000"), Decimal("100"), False, "fosun_hk")
    assert fees.stamp == Decimal("10"), "0.1% of 10,000 is 10"
    small = quote_fees("HK", Decimal("1005"), Decimal("10"), False, "fosun_hk")
    assert small.stamp == Decimal("2"), "1.005 must round up to 2, not down to 1"


def test_hk_cash_settlement_floor():
    fees = quote_fees("HK", Decimal("1000"), Decimal("10"), False, "fosun_hk")
    assert fees.settlement == Decimal("2"), "0.002% of 1,000 is 0.02, floor is 2"


def test_hk_round_trip_is_about_half_a_percent():
    total = round_trip("HK", Decimal("10000"), Decimal("100"), "fosun_hk")
    pct = total / Decimal("10000") * 100
    assert Decimal("0.5") < pct < Decimal("0.65"), f"unexpected {pct}%"


def test_small_hk_trade_is_expensive():
    total = round_trip("HK", Decimal("1000"), Decimal("10"), "fosun_hk")
    pct = total / Decimal("1000") * 100
    assert pct > Decimal("3"), "the fixed platform fee dominates small orders"


def test_us_sell_side_has_regulatory_fees():
    buy = quote_fees("US", Decimal("1000"), Decimal("10"), False, "fosun_us")
    sell = quote_fees("US", Decimal("1000"), Decimal("10"), True, "fosun_us")
    assert buy.platform == Decimal("1")
    assert sell.total > buy.total


def test_cn_stamp_only_on_sell():
    buy = quote_fees("CN", Decimal("10000"), Decimal("1000"), False, "cn_generic")
    sell = quote_fees("CN", Decimal("10000"), Decimal("1000"), True, "cn_generic")
    assert buy.stamp == 0
    assert sell.stamp == Decimal("5.0")


def test_all_plans_are_usable():
    for code, plan in PLANS.items():
        fees = quote_fees(plan.market, Decimal("10000"), Decimal("100"), False, code)
        assert fees.total >= 0
