"""Time helpers. Everything is stored in UTC; rendering uses the market timezone."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

MARKET_TZ = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).isoformat(timespec="seconds")


def today_utc() -> date:
    return utcnow().date()


def market_tz(market: str) -> ZoneInfo:
    return ZoneInfo(MARKET_TZ.get(market.upper(), "UTC"))


def in_market_tz(dt: datetime, market: str) -> datetime:
    return dt.astimezone(market_tz(market))


def market_now(market: str) -> datetime:
    return utcnow().astimezone(market_tz(market))


def session_bounds(market: str, day: date) -> tuple[datetime, datetime]:
    """Return the regular session open/close for a market on a given local date."""
    mkt = market.upper()
    if mkt == "US" or mkt == "HK":
        start, end = time(9, 30), time(16, 0)
    else:
        start, end = time(9, 30), time(15, 0)
    tz = market_tz(mkt)
    return (
        datetime.combine(day, start, tzinfo=tz),
        datetime.combine(day, end, tzinfo=tz),
    )
