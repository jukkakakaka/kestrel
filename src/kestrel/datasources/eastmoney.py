"""Eastmoney quote provider (primary source).

Endpoint returns UTF-8 JSON:
    https://push2.eastmoney.com/api/qt/stock/get?secid=116.00700&fields=...
``f59`` carries the price decimal count, so the divisor must be computed per
quote instead of being hard-coded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from kestrel.datasources.base import DataSourceError, Quote

BASE = "https://push2.eastmoney.com/api/qt/stock/get"
FIELDS = "f43,f44,f45,f46,f47,f48,f57,f58,f59,f60,f170"

MARKET_PREFIX = {"CN": ("1", "0"), "HK": ("116",), "US": ("105", "106")}


def _secids(market: str, code: str) -> list[str]:
    """Candidate secids, most specific first.

    A resolved secid (from ``kestrel.datasources.resolve``) wins, because the
    prefix-guessing below only covers the common cases. Guesses stay as a
    fallback so the app still works before a symbol has been resolved.
    """
    market = market.upper()
    out: list[str] = []
    if market == "CN":
        sh = code.startswith(("6", "9", "5"))
        out.append(f"1.{code}" if sh else f"0.{code}")
        out.append(f"0.{code}" if sh else f"1.{code}")
    elif market == "HK":
        out.append(f"116.{code}")
    else:
        out.extend(f"{p}.{code}" for p in MARKET_PREFIX["US"])

    try:
        from kestrel.datasources.resolve import known_secid

        secid = known_secid(market, code)
    except Exception:  # pragma: no cover - resolve is optional here
        secid = None
    if secid:
        out = [secid] + [s for s in out if s != secid]
    return out


def _decimal(value, digits: int) -> Decimal | None:
    if value is None or value == "-" or value == "":
        return None
    try:
        return Decimal(str(value)) / (Decimal(10) ** digits)
    except Exception:
        return None


def parse_payload(payload: dict, market: str, code: str) -> Quote | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    digits = int(data.get("f59") or 2)
    price = _decimal(data.get("f43"), digits)
    if price is None or price <= 0:
        return None
    return Quote(
        market=market,
        code=str(data.get("f57") or code),
        name=str(data.get("f58") or code),
        price=price,
        prev_close=_decimal(data.get("f60"), digits),
        open=_decimal(data.get("f46"), digits),
        high=_decimal(data.get("f44"), digits),
        low=_decimal(data.get("f45"), digits),
        volume=int(data["f47"]) if isinstance(data.get("f47"), (int, float)) else None,
        turnover=_decimal(data.get("f48"), 0),
        change_pct=(float(data["f170"]) / 100) if isinstance(data.get("f170"), (int, float)) else None,
        source="eastmoney",
        quoted_at=datetime.now(UTC),
    )


class EastmoneyProvider:
    name = "eastmoney"

    def __init__(self, timeout: float = 8.0) -> None:
        self._timeout = timeout

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.1)"}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            for market, code in symbols:
                for secid in _secids(market, code):
                    try:
                        resp = await client.get(BASE, params={"secid": secid, "fields": FIELDS})
                        resp.raise_for_status()
                        quote = parse_payload(resp.json(), market, code)
                    except Exception:
                        continue
                    if quote:
                        out[quote.key] = quote
                        break
        if not out and symbols:
            raise DataSourceError("eastmoney returned no usable quotes")
        return out
