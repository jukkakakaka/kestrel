"""Tencent quote provider (backup source).

``https://qt.gtimg.cn/q=sh600519,hk00700,usAAPL`` returns GBK encoded text.
Fields are ``~`` separated: 1 name, 2 code, 3 price, 4 prev close, 5 open,
30 timestamp, 31 change, 32 change %, 33 high, 34 low, 36 volume.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from kestrel.datasources.base import DataSourceError, Quote

BASE = "https://qt.gtimg.cn/q="

PREFIX = {"CN": "", "HK": "hk", "US": "us"}


def _qcode(market: str, code: str) -> str:
    market = market.upper()
    if market == "CN":
        return ("sh" if code.startswith(("6", "9", "5")) else "sz") + code
    if market == "HK":
        return "hk" + code
    return "us" + code


def _dec(text: str) -> Decimal | None:
    try:
        value = Decimal(text)
    except Exception:
        return None
    return value if value > 0 else None


def parse_text(raw: str) -> dict[str, dict]:
    """Parse the raw GBK text into ``{qcode: fields}``."""
    rows: dict[str, dict] = {}
    for line in raw.split(";"):
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        key, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        fields = payload.split("~")
        if len(fields) < 35:
            continue
        rows[key[2:]] = {
            "name": fields[1],
            "code": fields[2],
            "price": fields[3],
            "prev_close": fields[4],
            "open": fields[5],
            "time": fields[30],
            "change": fields[31],
            "change_pct": fields[32],
            "high": fields[33],
            "low": fields[34],
        }
    return rows


class TencentProvider:
    name = "tencent"

    def __init__(self, timeout: float = 8.0) -> None:
        self._timeout = timeout

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        if not symbols:
            return {}
        codes = [_qcode(m, c) for m, c in symbols]
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.1)"}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            resp = await client.get(BASE + ",".join(codes))
            resp.raise_for_status()
            raw = resp.content.decode("gbk", errors="replace")

        rows = parse_text(raw)
        out: dict[str, Quote] = {}
        for market, code in symbols:
            row = rows.get(_qcode(market, code))
            if not row:
                continue
            price = _dec(row["price"])
            if price is None:
                continue
            out[f"{market}:{code}"] = Quote(
                market=market,
                code=code,
                name=row["name"],
                price=price,
                prev_close=_dec(row["prev_close"]),
                open=_dec(row["open"]),
                high=_dec(row["high"]),
                low=_dec(row["low"]),
                change_pct=float(row["change_pct"]) if row["change_pct"] else None,
                source="tencent",
                quoted_at=datetime.now(UTC),
            )
        if not out:
            raise DataSourceError("tencent returned no usable quotes")
        return out
