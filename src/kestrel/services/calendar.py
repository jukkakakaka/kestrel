"""Trading sessions, holidays and the next open/close time per market.

Holiday lists are curated from exchange notices and intentionally kept in one
place so they are easy to correct. They are used for *display* (is the market
open, when does it open next), never for pricing decisions: an extra day of
"market open" changes nothing except a label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MARKET_TZ = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}

MARKET_NAME = {"CN": "A 股", "HK": "港股", "US": "美股"}

# (open, close) in local time. A market with a lunch break has two ranges.
SESSIONS: dict[str, list[tuple[time, time]]] = {
    "CN": [(time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))],
    "HK": [(time(9, 30), time(12, 0)), (time(13, 0), time(16, 0))],
    "US": [(time(9, 30), time(16, 0))],
}

# Full-day closures. Update once a year; wrong entries only affect a label.
HOLIDAYS: dict[str, set[str]] = {
    "US": {
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    },
    "HK": {
        "2026-01-01", "2026-02-17", "2026-02-18", "2026-02-19", "2026-04-03",
        "2026-04-06", "2026-05-01", "2026-05-25", "2026-06-19", "2026-07-01",
        "2026-10-01", "2026-10-19", "2026-12-25", "2026-12-26",
        "2027-01-01", "2027-02-05", "2027-02-08", "2027-03-26", "2027-03-29",
        "2027-04-05", "2027-05-01", "2027-05-13", "2027-06-09", "2027-07-01",
        "2027-09-16", "2027-10-01", "2027-10-08", "2027-12-25",
    },
    "CN": {
        "2026-01-01", "2026-01-02", "2026-02-16", "2026-02-17", "2026-02-18",
        "2026-02-19", "2026-02-20", "2026-04-06", "2026-05-01", "2026-05-04",
        "2026-05-05", "2026-06-19", "2026-09-25", "2026-10-01", "2026-10-02",
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2027-01-01", "2027-02-05", "2027-02-08", "2027-02-09", "2027-02-10",
        "2027-02-11", "2027-02-12", "2027-04-05", "2027-05-03", "2027-05-04",
        "2027-05-05", "2027-06-09", "2027-09-15", "2027-10-01", "2027-10-04",
        "2027-10-05", "2027-10-06", "2027-10-07",
    },
}


@dataclass(frozen=True)
class MarketStatus:
    market: str
    name: str
    state: str          # open / break / pre / after / closed / holiday
    label: str          # human readable, Chinese
    local_time: str
    next_open: datetime | None
    seconds_to_open: int | None

    @property
    def is_open(self) -> bool:
        return self.state == "open"


def _holidays(market: str) -> set[str]:
    return HOLIDAYS.get(market.upper(), set())


def is_trading_day(market: str, day: date) -> bool:
    if day.weekday() >= 5:
        return False
    return day.isoformat() not in _holidays(market)


def _session_state(market: str, moment: datetime) -> tuple[str, datetime | None]:
    """Return (state, next transition time)."""
    today = moment.date()
    t = moment.time()
    sessions = SESSIONS.get(market.upper(), [])

    if not is_trading_day(market, today):
        return "holiday" if today.weekday() < 5 else "closed", None

    for index, (opens, closes) in enumerate(sessions):
        if opens <= t < closes:
            return "open", datetime.combine(today, closes, tzinfo=moment.tzinfo)
        if t < opens:
            return "pre", datetime.combine(today, opens, tzinfo=moment.tzinfo)
        # between this close and the next open (lunch break)
        if index + 1 < len(sessions):
            nxt = sessions[index + 1][0]
            if t < nxt:
                return "break", datetime.combine(today, nxt, tzinfo=moment.tzinfo)
    return "after", None


def _next_open(market: str, moment: datetime) -> datetime:
    tz = ZoneInfo(MARKET_TZ.get(market.upper(), "UTC"))
    local = moment.astimezone(tz)
    day = local.date()
    for _ in range(30):
        if is_trading_day(market, day):
            for opens, _closes in SESSIONS.get(market.upper(), []):
                candidate = datetime.combine(day, opens, tzinfo=tz)
                if candidate > local:
                    return candidate
        day += timedelta(days=1)
    return local + timedelta(days=1)


_LABELS = {
    "open": "交易中",
    "break": "午间休市",
    "pre": "盘前",
    "after": "已收盘",
    "closed": "周末休市",
    "holiday": "休市",
}


def status(market: str) -> MarketStatus:
    market = market.upper()
    tz = ZoneInfo(MARKET_TZ.get(market, "UTC"))
    now = datetime.now(tz)
    state, _next = _session_state(market, now)
    nxt = _next_open(market, now)
    return MarketStatus(
        market=market,
        name=MARKET_NAME.get(market, market),
        state=state,
        label=_LABELS.get(state, state),
        local_time=now.strftime("%H:%M"),
        next_open=nxt,
        seconds_to_open=max(0, int((nxt - now).total_seconds())),
    )


def all_status() -> list[MarketStatus]:
    return [status(m) for m in ("CN", "HK", "US")]


def next_session_close(market: str) -> str:
    """ISO timestamp of the next tradable close, in UTC.

    Replaces the ad-hoc weekend logic that used to live in the plan builder.
    """
    market = market.upper()
    tz = ZoneInfo(MARKET_TZ.get(market, "UTC"))
    now = datetime.now(tz)
    sessions = SESSIONS.get(market, [(time(16, 0), time(16, 0))])
    last_close = sessions[-1][1]

    day = now.date()
    for _ in range(30):
        if is_trading_day(market, day):
            close = datetime.combine(day, last_close, tzinfo=tz)
            if close > now:
                return close.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        day += timedelta(days=1)
    return (now + timedelta(days=1)).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")


def last_closed_session(market: str) -> str:
    """ISO date of the most recent session that has already closed.

    Cache freshness has to be judged against this, not against "today minus N
    days": a four-day tolerance meant a Tuesday scan could happily run on last
    Friday's bars. Before the close the answer is the previous session; after it,
    today.
    """
    market = market.upper()
    tz = ZoneInfo(MARKET_TZ.get(market, "UTC"))
    now = datetime.now(tz)
    sessions = SESSIONS.get(market, [(time(16, 0), time(16, 0))])
    last_close = sessions[-1][1]

    day = now.date()
    for _ in range(30):
        if is_trading_day(market, day):
            close = datetime.combine(day, last_close, tzinfo=tz)
            if close <= now:
                return day.isoformat()
        day -= timedelta(days=1)
    return (now.date() - timedelta(days=1)).isoformat()


def upcoming_holidays(market: str, limit: int = 4) -> list[str]:
    today = date.today()
    days = sorted(d for d in _holidays(market.upper()) if d >= today.isoformat())
    return days[:limit]
