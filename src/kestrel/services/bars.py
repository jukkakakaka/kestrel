"""Daily bar cache: read from SQLite, refresh from the provider when stale."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from kestrel.core.money import to_micro
from kestrel.datasources.history import fetch_daily

log = logging.getLogger(__name__)


def _rows(conn: sqlite3.Connection, symbol_id: int, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume, turnover
        FROM bar_daily WHERE symbol_id=? ORDER BY trade_date DESC LIMIT ?
        """,
        (symbol_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        out.append(
            {
                "date": r["trade_date"],
                "open": r["open"] / 1_000_000,
                "high": r["high"] / 1_000_000,
                "low": r["low"] / 1_000_000,
                "close": r["close"] / 1_000_000,
                "volume": r["volume"] or 0,
                "turnover": (r["turnover"] or 0) / 1_000_000,
            }
        )
    return out


def _upsert(conn: sqlite3.Connection, symbol_id: int, bars: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO bar_daily (symbol_id, trade_date, open, high, low, close, volume, turnover)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(symbol_id, trade_date) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume, turnover=excluded.turnover
        """,
        [
            (
                symbol_id,
                str(b["date"]),
                to_micro(b["open"]),
                to_micro(b["high"]),
                to_micro(b["low"]),
                to_micro(b["close"]),
                int(b["volume"]),
                to_micro(b["turnover"]),
            )
            for b in bars
        ],
    )


def _depth(conn: sqlite3.Connection, symbol_id: int) -> int:
    row = conn.execute("SELECT bars_depth FROM symbol WHERE id=?", (symbol_id,)).fetchone()
    return int(row["bars_depth"] or 0) if row else 0


def _set_depth(conn: sqlite3.Connection, symbol_id: int, depth: int) -> None:
    conn.execute("UPDATE symbol SET bars_depth=? WHERE id=?", (int(depth), symbol_id))


def needs_fetch(
    conn: sqlite3.Connection, symbol_id: int, market: str, bars: list[dict], limit: int
) -> bool:
    """Should this symbol be re-fetched?

    Two different reasons: the data is old, or it is fresh but was never fetched
    this deep. Without the second check, switching to a monthly horizon would
    keep reading three years of dailies forever because nothing was "stale".
    """
    if is_stale(bars, market):
        return True
    return len(bars) < limit and _depth(conn, symbol_id) < limit


async def get_bars(
    conn: sqlite3.Connection,
    symbol_id: int,
    market: str,
    code: str,
    limit: int = 260,
) -> list[dict]:
    """Return cached bars, fetching when the cache is behind or too shallow."""
    cached = drop_forming(_rows(conn, symbol_id, limit), market)
    if cached and not needs_fetch(conn, symbol_id, market, cached, limit):
        return cached
    try:
        bars = await fetch_daily(market, code, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("bar refresh failed for %s:%s (%s)", market, code, exc)
        return cached
    if bars:
        save_bars(conn, symbol_id, bars, depth=limit)
        conn.commit()
    return drop_forming(_rows(conn, symbol_id, limit), market)


def cached_bars(conn: sqlite3.Connection, symbol_id: int, limit: int = 260) -> list[dict]:
    return _rows(conn, symbol_id, limit)


def drop_forming(bars: list[dict], market: str | None = None) -> list[dict]:
    """Drop a trailing bar for a session that has not closed yet.

    Providers start returning today's candle as soon as the market opens, so a
    mid-session refresh would otherwise let a half-built bar drive indicators —
    the signal appears at 10:00 and evaporates by the close. Everything in this
    app is a close-confirmed decision, so the forming bar has to go.
    """
    if not bars or not market or str(market).upper() not in ("CN", "HK", "US"):
        return bars
    from kestrel.services.calendar import last_closed_session

    if str(bars[-1]["date"])[:10] > last_closed_session(market):
        return bars[:-1]
    return bars


def is_stale(bars: list[dict], market: str | None = None) -> bool:
    """Is the cache behind the last session that has actually closed?

    The old check was ``today - 4 days > last_date``, which was meant to survive a
    long weekend but ended up letting a Tuesday scan run on Friday's bars. When
    the market is known we ask the trading calendar instead.
    """
    if not bars:
        return True
    last = str(bars[-1]["date"])[:10]
    if market and str(market).upper() in ("CN", "HK", "US"):
        from kestrel.services.calendar import last_closed_session

        return last < last_closed_session(market)
    return str(date.today() - timedelta(days=4)) > last


async def fetch_only(market: str, code: str, limit: int = 260) -> list[dict]:
    """Network fetch with no database access, safe to run concurrently."""
    try:
        return await fetch_daily(market, code, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("bar fetch failed for %s:%s (%s)", market, code, exc)
        return []


def save_bars(conn: sqlite3.Connection, symbol_id: int, bars: list[dict], depth: int = 0) -> None:
    if not bars:
        return
    _upsert(conn, symbol_id, bars)
    if depth:
        _set_depth(conn, symbol_id, depth)


def valid_count(bars: list[dict]) -> int:
    return len(bars)
