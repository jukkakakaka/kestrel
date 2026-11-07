"""Candidate universe: the stocks a scan is allowed to look at.

Scanning only the watchlist is not stock picking, it is a health check on
positions you already know about. This module pulls a liquid, size-ranked slice
of each market so the scanner has something to actually choose from.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from kestrel.db.engine import connect

log = logging.getLogger(__name__)

CLIST = "https://push2.eastmoney.com/api/qt/clist/get"

# fs selects the board; fid=f20 sorts by total market cap.
BOARDS = {
    "CN": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
    "HK": "m:116+t:3",
    "US": "m:105,m:106",
}

FIELDS = "f12,f14,f2,f3,f6,f20,f21,f100"


@dataclass(frozen=True)
class Candidate:
    market: str
    code: str
    name: str
    price: float
    change_pct: float
    turnover: float
    market_cap: float
    sector: str = ""


def _code_fits(market: str, code: str) -> bool:
    """Guard against cross-market contamination in the pool response."""
    if market == "CN":
        return code.isdigit() and len(code) == 6
    if market == "HK":
        return code.isdigit() and 1 <= len(code) <= 5
    return code.isalpha() and 1 <= len(code) <= 6


async def fetch_pool(market: str, size: int = 40, timeout: float = 12.0) -> list[Candidate]:
    """Top ``size`` names by market cap for one market."""
    board = BOARDS.get(market.upper())
    if not board:
        return []
    params = {
        "pn": 1,
        "pz": max(10, min(size, 100)),
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f20",
        "fs": board,
        "fields": FIELDS,
    }
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.2)"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(CLIST, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("pool fetch failed for %s: %s", market, exc)
        return []

    rows = ((payload or {}).get("data") or {}).get("diff") or []
    out: list[Candidate] = []
    for row in rows:
        code = str(row.get("f12") or "").strip()
        name = str(row.get("f14") or "").strip()
        price = row.get("f2")
        if not code or not name or not isinstance(price, (int, float)) or price <= 0:
            continue
        if not _code_fits(market.upper(), code):
            continue
        # skip suspended / non-tradable and obvious special vehicles
        if "ST" in name.upper() or "退" in name or name.endswith("-R"):
            continue
        cap = row.get("f20")
        turnover = row.get("f6")
        out.append(
            Candidate(
                market=market.upper(),
                code=code,
                name=name,
                price=float(price),
                change_pct=float(row.get("f3") or 0),
                turnover=float(turnover) if isinstance(turnover, (int, float)) else 0.0,
                market_cap=float(cap) if isinstance(cap, (int, float)) else 0.0,
                sector=str(row.get("f100") or "").strip(),
            )
        )
    return out


def store_pool(candidates: list[Candidate]) -> int:
    """Persist the pool so the scan does not refetch it every minute."""
    if not candidates:
        return 0
    conn = connect()
    try:
        conn.execute("BEGIN")
        for rank, item in enumerate(candidates, start=1):
            currency = {"CN": "CNY", "HK": "HKD", "US": "USD"}[item.market]
            lot = 100 if item.market in ("CN", "HK") else 1
            conn.execute(
                """
                INSERT INTO symbol (market, code, name, currency, lot_size, is_pool,
                                    pool_rank, pool_cap, sector, updated_at)
                VALUES (?,?,?,?,?,1,?,?,?,datetime('now'))
                ON CONFLICT(market, code) DO UPDATE SET
                    name = excluded.name,
                    is_pool = 1,
                    pool_rank = excluded.pool_rank,
                    pool_cap = excluded.pool_cap,
                    sector = COALESCE(NULLIF(excluded.sector, ''), symbol.sector),
                    updated_at = datetime('now')
                """,
                (
                    item.market,
                    item.code,
                    item.name,
                    currency,
                    lot,
                    rank,
                    int(item.market_cap),
                    item.sector,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return len(candidates)


def clear_pool(market: str | None = None) -> None:
    conn = connect()
    try:
        if market:
            conn.execute("UPDATE symbol SET is_pool=0 WHERE market=?", (market.upper(),))
        else:
            conn.execute("UPDATE symbol SET is_pool=0")
        conn.commit()
    finally:
        conn.close()


def pool_symbols(markets: list[str], limit_per_market: int = 40) -> list[dict]:
    """Candidates currently in the database, best rank first."""
    if not markets:
        return []
    conn = connect()
    try:
        marks = ",".join("?" * len(markets))
        rows = conn.execute(
            f"""
            SELECT id, market, code, name, currency, lot_size, secid, pool_rank, sector
            FROM symbol
            WHERE is_pool=1 AND market IN ({marks}) AND pool_rank <= ?
            ORDER BY pool_rank
            """,
            (*[m.upper() for m in markets], limit_per_market),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


async def refresh(markets: list[str], size: int = 40) -> dict[str, int]:
    """Refresh the stored pool for the given markets."""
    counts: dict[str, int] = {}
    for market in markets:
        candidates = await fetch_pool(market, size)
        clear_pool(market)
        counts[market] = store_pool(candidates)
    return counts


def pool_summary() -> dict[str, int]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT market, COUNT(*) AS n FROM symbol WHERE is_pool=1 GROUP BY market"
        ).fetchall()
        return {r["market"]: r["n"] for r in rows}
    finally:
        conn.close()


def to_json(items: list[Candidate]) -> str:
    return json.dumps([c.__dict__ for c in items], ensure_ascii=False)
