"""Daily K-line fetch for the Eastmoney history endpoint."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

from kestrel.datasources.base import DataSourceError
from kestrel.datasources.eastmoney import _secids

HISTORY = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"


def parse_klines(payload: dict) -> list[dict]:
    """Parse ``data.klines`` rows: date,open,close,high,low,volume,turnover."""
    block = payload.get("data") or {}
    rows = block.get("klines") or []
    out: list[dict] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 7:
            continue
        try:
            out.append(
                {
                    "date": date.fromisoformat(parts[0]),
                    "open": Decimal(parts[1]),
                    "close": Decimal(parts[2]),
                    "high": Decimal(parts[3]),
                    "low": Decimal(parts[4]),
                    "volume": int(Decimal(parts[5])),
                    "turnover": Decimal(parts[6]),
                }
            )
        except Exception:
            continue
    return out


async def fetch_daily(
    market: str, code: str, limit: int = 260, timeout: float = 12.0
) -> list[dict]:
    """Return up to ``limit`` most recent daily bars, oldest first."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.1)"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for secid in _secids(market, code):
            try:
                resp = await client.get(
                    HISTORY,
                    params={
                        "secid": secid,
                        "fields1": FIELDS1,
                        "fields2": FIELDS2,
                        "klt": 101,
                        "fqt": 1,
                        "end": "20500101",
                        "lmt": limit,
                    },
                )
                resp.raise_for_status()
                bars = parse_klines(resp.json())
            except Exception:
                continue
            if bars:
                return bars
    raise DataSourceError(f"no daily bars for {market}:{code}")
