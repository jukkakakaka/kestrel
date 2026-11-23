"""Fetch a complete daily OHLCV series from one provider at a time."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from kestrel.core.clock import market_tz, utcnow
from kestrel.datasources.base import DataSourceError, cn_exchange_prefix, normalize_code
from kestrel.datasources.eastmoney import _secids

log = logging.getLogger(__name__)

HISTORY = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"


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
    return sorted(out, key=lambda bar: bar["date"])


def _yahoo_ticker(market: str, code: str) -> str | None:
    code = normalize_code(market, code)
    if market == "US":
        return code.replace(".", "-")
    if market == "HK":
        return code.lstrip("0").zfill(4) + ".HK"
    if market == "CN":
        suffix = {"sh": ".SS", "sz": ".SZ"}.get(cn_exchange_prefix(code))
        return code + suffix if suffix else None
    return None


def _parse_yahoo(payload: dict, market: str) -> list[dict]:
    results = (payload.get("chart") or {}).get("result") or []
    if not results:
        return []
    chart = results[0]
    quotes = (chart.get("indicators") or {}).get("quote") or []
    if not quotes:
        return []

    quote = quotes[0]
    days = {}
    zone = market_tz(market)
    for i, timestamp in enumerate(chart.get("timestamp") or []):
        try:
            day = datetime.fromtimestamp(timestamp, tz=zone).date()
            prices = {field: Decimal(str(quote[field][i])) for field in ("open", "high", "low", "close")}
            if any(not value.is_finite() or value <= 0 for value in prices.values()):
                continue
            if not (
                prices["low"] <= min(prices["open"], prices["close"]) and
                prices["high"] >= max(prices["open"], prices["close"])
            ):
                continue
            volume = int(quote["volume"][i] or 0)
            days[day] = {"date": day, **prices, "volume": max(0, volume), "turnover": Decimal("0")}
        except (IndexError, KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            continue
    return [days[day] for day in sorted(days)]


async def fetch_daily(
    market: str, code: str, limit: int = 260, timeout: float = 12.0,
) -> list[dict]:
    """Return the latest series, ordered oldest first, without mixing adjustments."""
    from kestrel.services.calendar import last_closed_session

    market = market.upper()
    end = utcnow()
    last_session = date.fromisoformat(last_closed_session(market))
    primary: list[dict] = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel)"}
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for secid in _secids(market, code):
            try:
                resp = await client.get(
                    HISTORY,
                    params={
                        "secid": secid, "fields1": FIELDS1, "fields2": FIELDS2,
                        "klt": 101, "fqt": 1, "end": last_session.strftime("%Y%m%d"), "lmt": limit,
                    },
                )
                resp.raise_for_status()
                primary = [bar for bar in parse_klines(resp.json()) if bar["date"] <= last_session]
            except (ValueError, httpx.HTTPError) as exc:
                log.debug("Eastmoney daily bars for %s: %s", secid, exc)
                if isinstance(exc, httpx.RequestError):
                    break
            if primary:
                if primary[-1]["date"] >= last_session and len(primary) >= limit:
                    return [bar | {"source": "eastmoney"} for bar in primary[-limit:]]
                break

        ticker = _yahoo_ticker(market, code)
        if ticker:
            try:
                response = await client.get(
                    YAHOO_CHART + ticker,
                    params={
                        "period1": int((end - timedelta(days=max(15, limit * 2))).timestamp()),
                        "period2": int(end.timestamp()), "interval": "1d",
                    },
                )
                response.raise_for_status()
                fallback = [bar for bar in _parse_yahoo(response.json(), market) if bar["date"] <= last_session]
                if fallback and (
                    not primary
                    or fallback[-1]["date"] > primary[-1]["date"]
                    or (fallback[-1]["date"] >= last_session and len(fallback) > len(primary))
                ):
                    return [bar | {"source": "yahoo"} for bar in fallback[-limit:]]
            except (ValueError, httpx.HTTPError) as exc:
                log.debug("Yahoo daily bars for %s: %s", ticker, exc)
    if primary:
        return [bar | {"source": "eastmoney"} for bar in primary[-limit:]]
    raise DataSourceError(f"no daily bars for {market}:{code}")
