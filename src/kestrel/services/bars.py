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


async def get_bars(
    conn: sqlite3.Connection,
    symbol_id: int,
    market: str,
    code: str,
    limit: int = 260,
) -> list[dict]:
    """Return cached bars, fetching when the cache is empty or older than a day."""
    cached = _rows(conn, symbol_id, limit)
    latest = cached[-1]["date"] if cached else None
    stale = latest is None or str(date.today() - timedelta(days=4)) > str(latest)
    if cached and not stale:
        return cached
    try:
        bars = await fetch_daily(market, code, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("bar refresh failed for %s:%s (%s)", market, code, exc)
        return cached
    if bars:
        _upsert(conn, symbol_id, bars)
        conn.commit()
    return _rows(conn, symbol_id, limit)


def cached_bars(conn: sqlite3.Connection, symbol_id: int, limit: int = 260) -> list[dict]:
    return _rows(conn, symbol_id, limit)


def is_stale(bars: list[dict]) -> bool:
    if not bars:
        return True
    return str(date.today() - timedelta(days=4)) > str(bars[-1]["date"])


async def fetch_only(market: str, code: str, limit: int = 260) -> list[dict]:
    """Network fetch with no database access, safe to run concurrently."""
    try:
        return await fetch_daily(market, code, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("bar fetch failed for %s:%s (%s)", market, code, exc)
        return []


def save_bars(conn: sqlite3.Connection, symbol_id: int, bars: list[dict]) -> None:
    if bars:
        _upsert(conn, symbol_id, bars)


def valid_count(bars: list[dict]) -> int:
    return len(bars)
