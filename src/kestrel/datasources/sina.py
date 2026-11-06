"""Sina quote provider (backup source).

Requires a Referer header, returns GBK text. Field layout differs per market:

* CN ``name,open,prev_close,price,high,low,...``
* HK ``name_en,name_cn,open,prev_close,high,low,price,change,change_pct,...``
* US ``name,price,change_pct,time,change,open,high,low,...``
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from kestrel.datasources.base import DataSourceError, Quote

BASE = "https://hq.sinajs.cn/list="
HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.1)",
}


def _qcode(market: str, code: str) -> str:
    market = market.upper()
    if market == "CN":
        return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code
    if market == "HK":
        return "hk" + code
    return "gb_" + code.lower()


def _dec(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except Exception:
        return None
    return value if value > 0 else None


def parse_line(market: str, raw: str) -> dict | None:
    parts = raw.split(",")
    try:
        if market == "CN" and len(parts) >= 6:
            return {"name": parts[0], "open": parts[1], "prev_close": parts[2],
                    "price": parts[3], "high": parts[4], "low": parts[5]}
        if market == "HK" and len(parts) >= 9:
            return {"name": parts[1], "open": parts[2], "prev_close": parts[3],
                    "high": parts[4], "low": parts[5], "price": parts[6],
                    "change_pct": parts[8]}
        if market == "US" and len(parts) >= 8:
            return {"name": parts[0], "price": parts[1], "change_pct": parts[2],
                    "open": parts[5], "high": parts[6], "low": parts[7]}
    except IndexError:
        return None
    return None


class SinaProvider:
    name = "sina"

    def __init__(self, timeout: float = 8.0) -> None:
        self._timeout = timeout

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        if not symbols:
            return {}
        codes = [_qcode(m, c) for m, c in symbols]
        async with httpx.AsyncClient(timeout=self._timeout, headers=HEADERS) as client:
            resp = await client.get(BASE + ",".join(codes))
            resp.raise_for_status()
            raw = resp.content.decode("gbk", errors="replace")

        rows: dict[str, str] = {}
        for line in raw.splitlines():
            if "hq_str_" not in line or "=" not in line:
                continue
            key = line.split("hq_str_", 1)[1].split("=", 1)[0]
            value = line.split('"')[1] if '"' in line else ""
            if value:
                rows[key] = value

        out: dict[str, Quote] = {}
        for market, code in symbols:
            row = parse_line(market, rows.get(_qcode(market, code), ""))
            if not row:
                continue
            price = _dec(str(row.get("price", "")))
            if price is None:
                continue
            pct = row.get("change_pct")
            out[f"{market}:{code}"] = Quote(
                market=market,
                code=code,
                name=row.get("name") or code,
                price=price,
                prev_close=_dec(str(row.get("prev_close", ""))),
                open=_dec(str(row.get("open", ""))),
                high=_dec(str(row.get("high", ""))),
                low=_dec(str(row.get("low", ""))),
                change_pct=float(pct) if pct else None,
                source="sina",
                quoted_at=datetime.now(UTC),
            )
        if not out:
            raise DataSourceError("sina returned no usable quotes")
        return out
