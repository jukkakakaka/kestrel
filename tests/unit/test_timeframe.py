"""Horizons: bar folding, plan lifetime, and the stop band that travels with them.

These pin the parts that are easy to get subtly wrong: folding a week must keep
the volume sum and the last close, the final (still forming) period must stay,
and a monthly plan must not inherit the daily stop ceiling.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from kestrel.planning.builder import build_ex, valid_until
from kestrel.services import timeframe
from kestrel.strategies.base import StrategyResult


def _daily(start: date, days: int) -> list[dict]:
    out = []
    price = 10.0
    for i in range(days):
        price += 0.1
        out.append(
            {
                "date": str(start + timedelta(days=i)),
                "open": price - 0.05,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price,
                "volume": 1000,
                "turnover": price * 1000,
            }
        )
    return out


def test_short_horizon_is_daily_and_untouched():
    bars = _daily(date(2026, 3, 2), 5)
    assert timeframe.prepare(bars, "short") is bars


def test_weekly_fold_takes_open_high_low_close_and_sums_volume():
    # 2026-03-02 is a Monday; the first five bars are one ISO week.
    bars = _daily(date(2026, 3, 2), 5)
    weekly = timeframe.aggregate(bars, "W")

    assert len(weekly) == 1
    week = weekly[0]
    assert week["open"] == bars[0]["open"]
    assert week["close"] == bars[-1]["close"]
    assert week["high"] == max(b["high"] for b in bars)
    assert week["low"] == min(b["low"] for b in bars)
    assert week["volume"] == sum(b["volume"] for b in bars)
    assert week["date"] == bars[-1]["date"]


def test_incomplete_last_week_is_kept():
    # Mon 2026-03-02 for 11 days: one full week plus a partial second one. The
    # partial week is what a live scan has to decide on, so it must survive.
    bars = _daily(date(2026, 3, 2), 11)
    weekly = timeframe.aggregate(bars, "W")
    assert len(weekly) == 2
    assert weekly[-1]["date"] == bars[-1]["date"]
    assert weekly[-1]["date"] > bars[4]["date"]  # started in the next week


def test_monthly_fold_uses_calendar_months():
    bars = _daily(date(2026, 1, 20), 40)  # Jan 20 → Feb 28
    monthly = timeframe.aggregate(bars, "M")
    assert len(monthly) == 2
    assert monthly[0]["close"] == bars[11]["close"]  # last trading day of January


def test_aggregate_is_idempotent_on_daily():
    bars = _daily(date(2026, 3, 2), 6)
    assert timeframe.aggregate(bars, "D") is bars


def test_unknown_horizon_falls_back_to_daily():
    assert timeframe.get("nonsense").code == timeframe.DEFAULT
    assert timeframe.get(None).bar == "D"


def test_monthly_stop_band_is_wider_than_daily():
    assert timeframe.get("long").stop_band[1] > timeframe.get("short").stop_band[1]
    assert timeframe.get("medium").stop_band[0] > timeframe.get("short").stop_band[0]


def test_longer_horizon_keeps_the_plan_alive_longer():
    short = valid_until("HK", "short")
    medium = valid_until("HK", "medium")
    long = valid_until("HK", "long")
    assert short < medium < long


def _signal() -> StrategyResult:
    return StrategyResult(code="trend_pullback", score=0.7, reasons=["trend_up"])


def _ind(atr: float) -> dict:
    return {"last": 100.0, "atr14": atr, "low20": 90.0, "high20": 130.0}


def _build(horizon: str, atr: float = 9.0):
    return build_ex(
        market="CN",
        code="600000",
        name="测试",
        ind=_ind(atr),
        signal=_signal(),
        capital_cny=100_000,
        risk_pct=2.0,
        lot_size=100,
        broker=None,
        horizon=horizon,
    )


def test_daily_rejects_a_stop_that_a_monthly_plan_accepts():
    # atr14 = 9 puts the stop ~17.7% below entry: too wide for daily swing, fine
    # on monthly bars where that is one ordinary month.
    plan, why = _build("short")
    assert plan is None
    assert why == "stop_too_wide"

    plan, why = _build("long")
    assert why is None
    assert plan is not None


def test_plan_cost_is_converted_to_cny():
    from kestrel.services.scan import _plan_cost_cny

    plan = SimpleNamespace(entry_low=100, qty=10, currency="USD")
    assert float(_plan_cost_cny(plan)) == pytest.approx(7100.0)


def test_exposure_ceiling_stops_the_batch_not_each_plan():
    from decimal import Decimal

    from kestrel.services.scan import _exceeds_exposure

    cap, used, added = Decimal("6000"), Decimal("1000"), Decimal("3000")
    assert not _exceeds_exposure(Decimal("2000"), cap, used, added)  # exactly full
    assert _exceeds_exposure(Decimal("2001"), cap, used, added)  # one more yuan


def test_unset_exposure_ceiling_blocks_nothing():
    from decimal import Decimal

    from kestrel.services.scan import _exceeds_exposure

    assert not _exceeds_exposure(Decimal("100000"), Decimal("0"), Decimal("0"), Decimal("0"))


def test_forming_bar_is_dropped_until_the_session_closes():
    from kestrel.services.bars import drop_forming

    future = [{"date": "2999-01-01", "open": 1, "high": 1, "low": 1, "close": 1}]
    assert drop_forming(future, "CN") == []

    closed = [{"date": "2000-01-03", "open": 1, "high": 1, "low": 1, "close": 1}]
    assert drop_forming(closed, "CN") == closed
