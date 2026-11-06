"""End-to-end check of strategy -> indicators -> plan, using synthetic bars."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from kestrel.indicators.engine import Series, compute
from kestrel.planning.builder import build
from kestrel.strategies.base import Breakout20

LOT = 100
BASE = 20.0  # low enough that one lot fits a 10k CNY account


def _bars(prices: list[float], volumes: list[float] | None = None) -> list[dict]:
    start = date(2026, 1, 1)
    volumes = volumes or [1_000_000] * len(prices)
    return [
        {
            "date": str(start + timedelta(days=i)),
            "open": price * 0.998,
            "high": price * 1.006,
            "low": price * 0.994,
            "close": price,
            "volume": volumes[i],
            "turnover": price * volumes[i],
        }
        for i, price in enumerate(prices)
    ]


def _uptrend_then_breakout() -> list[dict]:
    prices = [BASE + i * 0.06 for i in range(80)]
    vols = [1_000_000] * 80
    prices.append(prices[-1] * 1.04)
    vols.append(3_000_000)
    return _bars(prices, vols)


def _plan(risk_pct: float = 2.0):
    ind = compute(Series.from_bars(_uptrend_then_breakout()))
    assert ind, "indicators should compute"
    signal = Breakout20().evaluate(ind)
    assert signal is not None, "breakout should fire on a new high with volume"
    return build(
        market="HK",
        code="TEST",
        name="Test",
        ind=ind,
        signal=signal,
        capital_cny=10000,
        risk_pct=risk_pct,
        lot_size=LOT,
        broker="fosun_hk",
    )


def test_breakout_fires_and_plan_is_executable():
    plan = _plan()
    assert plan is not None, "a valid plan should be produced"
    assert plan.qty > 0
    assert plan.qty % LOT == 0, "quantity must respect the lot size"
    assert plan.stop < plan.entry_low < plan.target1
    assert plan.breakeven >= plan.entry_low
    assert 1.5 <= plan.r_multiple <= 5
    assert plan.valid_until.endswith("+00:00")
    assert "错了怎么办" in plan.explain_zh and "if wrong" in plan.explain_en.lower()
    assert "000000" not in plan.explain_zh, "prices must be formatted, not raw Decimals"


def test_risk_budget_caps_the_position():
    plan = _plan(risk_pct=1.0)
    assert plan is not None
    capital_hkd = Decimal("10000") / Decimal("0.92")
    budget = capital_hkd * Decimal("0.01")
    loss = (plan.entry_low - plan.stop) * plan.qty
    assert loss <= budget * Decimal("1.2"), "loss should stay near the risk budget"
    assert plan.qty * plan.entry_low <= capital_hkd * Decimal("0.36")


def test_no_plan_when_one_lot_exceeds_risk_budget():
    plan = _plan(risk_pct=0.2)
    assert plan is None, "one lot must not be forced when it breaks the budget"
