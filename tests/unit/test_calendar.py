"""Trading calendar: sessions, weekends, and the next tradable close."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from kestrel.services import calendar


def test_weekend_is_not_a_trading_day():
    assert not calendar.is_trading_day("US", date(2026, 11, 7))  # Saturday
    assert not calendar.is_trading_day("US", date(2026, 11, 8))  # Sunday
    assert calendar.is_trading_day("US", date(2026, 11, 9))      # Monday


def test_holiday_is_not_a_trading_day():
    assert not calendar.is_trading_day("US", date(2026, 11, 26))  # Thanksgiving
    assert not calendar.is_trading_day("HK", date(2026, 10, 1))   # National Day


def test_status_has_a_label_and_next_open():
    for market in ("CN", "HK", "US"):
        state = calendar.status(market)
        assert state.state in {"open", "break", "pre", "after", "closed", "holiday"}
        assert state.label
        assert state.next_open is not None
        assert state.seconds_to_open >= 0


def test_next_close_is_in_the_future_and_on_a_trading_day():
    for market in ("CN", "HK", "US"):
        moment = datetime.fromisoformat(calendar.next_session_close(market))
        tz = ZoneInfo(calendar.MARKET_TZ[market])
        local = moment.astimezone(tz)
        assert local > datetime.now(tz)
        assert calendar.is_trading_day(market, local.date())
        assert local.time() == calendar.SESSIONS[market][-1][1]


@pytest.mark.parametrize("market", ["CN", "HK", "US"])
def test_upcoming_holidays_are_sorted(market):
    days = calendar.upcoming_holidays(market, 5)
    assert days == sorted(days)
