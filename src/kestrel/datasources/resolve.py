"""Resolve a user-typed ticker into a canonical Eastmoney security id.

Guessing exchange prefixes ("105." / "106.") works for the popular tickers but
fails silently for everything else, which then shows up as "scanned 3 of 6".
This module asks Eastmoney's suggestion endpoint for the real ``QuoteID`` and
remembers it, so quotes and daily bars always hit the right security.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

SUGGEST = "https://searchapi.eastmoney.com/api/suggest/get"
TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

# Eastmoney spells the board, not the market: "沪A" / "深A" / "京A" / "港股" / "美股".
_MARKET_CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}


def _market_of(type_name: str) -> str | None:
    t = (type_name or "").strip()
    if not t:
        return None
    if "美股" in t:
        return "US"
    if "港股" in t:
        return "HK"
    if t in ("A股",) or t.endswith("A") or "A股" in t:
        return "CN"
    if any(board in t for board in ("科创板", "创业板", "北交所", "新三板")):
        return "CN"
    return None

# market:code -> secid, seeded from the database at startup
_SECIDS: dict[str, str] = {}


@dataclass(frozen=True)
class Resolved:
    market: str
    code: str
    name: str
    secid: str
    exchange: str
    currency: str
    lot_size: int


def remember(market: str, code: str, secid: str | None) -> None:
    key = f"{market.upper()}:{code.upper()}"
    if secid:
        _SECIDS[key] = secid
    else:
        _SECIDS.pop(key, None)


def forget(market: str, code: str) -> None:
    _SECIDS.pop(f"{market.upper()}:{code.upper()}", None)


def known_secid(market: str, code: str) -> str | None:
    return _SECIDS.get(f"{market.upper()}:{code.upper()}")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _exchange_name(market: str, raw: str, secid: str) -> str:
    """Eastmoney's ``JYS`` is a market code ("2") for A-shares; normalise it."""
    if market == "CN":
        return "上交所" if secid.startswith("1.") else "深交所"
    if market == "HK":
        return "港交所"
    return (raw or "").strip()


def _valid_code(market: str, code: str) -> bool:
    """Reject codes that cannot belong to the market, before they reach the DB."""
    if market == "CN":
        return code.isdigit() and len(code) == 6
    if market == "HK":
        return code.isdigit() and 1 <= len(code) <= 5
    return code.isalpha() and 1 <= len(code) <= 6


def load_from_db() -> int:
    """Seed the cache from the symbol table. Safe to call more than once."""
    from kestrel.db.engine import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT market, code, secid FROM symbol WHERE secid IS NOT NULL AND secid <> ''"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        _SECIDS[f"{row['market']}:{row['code'].upper()}"] = row["secid"]
    return len(rows)


async def resolve(query: str, market: str | None = None, timeout: float = 8.0) -> Resolved | None:
    """Look a ticker up. Returns the best match, or None if nothing plausible."""
    q = (query or "").strip()
    if not q:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.1)"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(
                SUGGEST,
                params={"input": q, "type": 14, "token": TOKEN, "count": 20},
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - network shape varies
        log.warning("symbol lookup failed for %r: %s", q, exc)
        return None

    table = (payload or {}).get("QuotationCodeTable") or {}
    items = table.get("Data") or []

    want = market.upper() if market else None
    upper_q = q.upper()
    scored: list[tuple[int, Resolved]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mkt = _market_of(str(item.get("SecurityTypeName") or ""))
        if mkt is None or (want and mkt != want):
            continue
        code = str(item.get("Code") or "").strip()
        secid = str(item.get("QuoteID") or "").strip()
        if not code or not secid:
            continue
        if not _valid_code(mkt, code):
            continue
        name = str(item.get("Name") or code).strip()
        resolved = Resolved(
            market=mkt,
            code=code,
            name=name,
            secid=secid,
            exchange=_exchange_name(mkt, str(item.get("JYS") or ""), secid),
            currency=_MARKET_CURRENCY[mkt],
            lot_size=100 if mkt in ("HK", "CN") else 1,
        )
        # Only trust an exact ticker hit or a name that begins with the query.
        # A loose "contains" match happily returns an unrelated fund, and a wrong
        # symbol that looks resolved is worse than a clear "not found".
        if code.upper() == upper_q:
            rank = 0
        elif name.upper() == upper_q:
            rank = 1
        elif name.upper().startswith(upper_q):
            rank = 2
        elif _has_cjk(upper_q) and upper_q in name.upper():
            # Chinese names are distinctive enough for a contains match, and
            # "茅台" has to find "贵州茅台". ASCII tickers stay strict.
            rank = 3
        else:
            continue
        # For name lookups prefer the home markets: "腾讯" should be 00700, not TME.
        home = {"CN": 0, "HK": 1, "US": 2}[mkt]
        scored.append((rank, home, len(code), resolved))

    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    best = scored[0][3]
    remember(best.market, best.code, best.secid)
    return best
