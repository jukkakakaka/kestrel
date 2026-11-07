"""A data source the user defines themselves.

The three built-in providers cover the common case, but they are unofficial
endpoints: when one of them starts returning junk there is nothing to switch to.
This lets a user point the app at any HTTP JSON (or plain-text) endpoint and map
the response fields by path, without touching code.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import httpx

from kestrel.datasources.base import DataSourceError, Quote

log = logging.getLogger(__name__)

_NUM = re.compile(r"-?\d+(?:\.\d+)?")

# A path is a sequence of keys and indices:
#   data.quote.price          -> {"data": {"quote": {"price": ...}}}
#   items[0].last             -> list access
#   Global Quote["05. price"] -> Alpha Vantage's keys contain spaces and dots
_PATH_TOKEN = re.compile(r"""\[(?:'([^']*)'|"([^"]*)"|([^\]]*))\]|([^.\[\]]+)""")


def _path_tokens(path: str) -> list[str]:
    tokens: list[str] = []
    for match in _PATH_TOKEN.finditer(path):
        single, double, bare, plain = match.groups()
        token = next((v for v in (single, double, bare, plain) if v is not None), "")
        token = token.strip()
        if token:
            tokens.append(token)
    return tokens


def _dig(payload, path: str):
    """Walk a path like ``data.quote.price``, ``items[0].last`` or ``["05. price"]``."""
    if not path:
        return None
    node = payload
    for part in _path_tokens(path):
        if isinstance(node, list):
            try:
                index = int(part)
            except ValueError:
                return None
            if index < 0 or index >= len(node):
                return None
            node = node[index]
        elif isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        else:
            return None
    return node


def _yahoo_symbol(market: str, code: str) -> str:
    """Yahoo wants 0700.HK / 600519.SS / 000001.SZ instead of a bare ticker."""
    if market == "HK":
        # we store 00700; Yahoo wants four digits
        digits = code.lstrip("0") or "0"
        return f"{digits.zfill(4)}.HK"
    if market == "CN":
        return f"{code}.SS" if code.startswith(("6", "9", "5")) else f"{code}.SZ"
    return code


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    match = _NUM.search(str(value).replace(",", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


class CustomProvider:
    """Configured entirely from a row in ``data_source``."""

    def __init__(self, config: dict) -> None:
        self.name = str(config.get("key") or "custom")
        self.label = str(config.get("label") or self.name)
        self.url_template = str(config.get("url") or "")
        self.markets = [m.strip().upper() for m in str(config.get("markets") or "").split(",") if m.strip()]
        self.price_path = str(config.get("price_path") or "price")
        self.prev_close_path = str(config.get("prev_close_path") or "prev_close")
        self.change_path = str(config.get("change_path") or "change_pct")
        self.name_path = str(config.get("name_path") or "name")
        self.currency = str(config.get("currency") or "")
        self.timeout = float(config.get("timeout") or 8.0)
        self.api_key = str(config.get("api_key") or "")
        self.method = str(config.get("method") or "GET").upper()
        self.body_template = str(config.get("body") or "")
        # Some endpoints return integers scaled by 10^n (Eastmoney uses milli-units).
        try:
            self.scale = Decimal(str(config.get("scale") or 1)) or Decimal("1")
        except Exception:  # noqa: BLE001
            self.scale = Decimal("1")
        self.headers = config.get("headers") or {}

    def supports(self, market: str) -> bool:
        return not self.markets or market.upper() in self.markets

    def _url_for(self, market: str, code: str) -> str:
        market = market.upper()
        url = (
            self.url_template.replace("{code}", code)
            .replace("{market}", market)
            .replace("{symbol}", f"{market}:{code}")
            .replace("{yf_symbol}", _yahoo_symbol(market, code))
            .replace("{key}", self.api_key)
        )
        return url

    async def fetch(self, symbols: list[tuple[str, str]]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        headers = {"User-Agent": "Mozilla/5.0 (compatible; Kestrel/0.2)"}
        headers.update({str(k): str(v) for k, v in (self.headers or {}).items()})

        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            for market, code in symbols:
                if not self.supports(market):
                    continue
                url = self._url_for(market, code)
                if not url.startswith("http"):
                    continue
                try:
                    if self.method == "POST":
                        payload_body = (
                            self.body_template.replace("{code}", code).replace("{key}", self.api_key)
                        )
                        resp = await client.post(
                            url,
                            content=payload_body or None,
                            headers={"Content-Type": "application/json"},
                        )
                    else:
                        resp = await client.get(url)
                    resp.raise_for_status()
                    body = resp.text
                except Exception as exc:  # noqa: BLE001
                    log.warning("custom source %s failed for %s: %s", self.name, code, exc)
                    continue

                payload = None
                try:
                    payload = json.loads(body)
                except (TypeError, ValueError):
                    payload = None

                price = _to_decimal(_dig(payload, self.price_path)) if payload is not None else _to_decimal(body)
                if price is None or price <= 0:
                    continue
                if self.scale and self.scale != 1:
                    price = price / self.scale

                prev_close = _to_decimal(_dig(payload, self.prev_close_path)) if payload else None
                if prev_close is not None and self.scale and self.scale != 1:
                    prev_close = prev_close / self.scale
                change = _to_decimal(_dig(payload, self.change_path)) if payload else None
                if change is None and prev_close and prev_close > 0:
                    change = (price - prev_close) / prev_close * 100

                display = _dig(payload, self.name_path) if payload else None
                out[f"{market.upper()}:{code}"] = Quote(
                    market=market.upper(),
                    code=code,
                    name=str(display or code),
                    price=price,
                    prev_close=prev_close,
                    change_pct=float(change) if change is not None else None,
                    source=self.name,
                    quoted_at=datetime.now(UTC),
                )

        if not out and symbols:
            raise DataSourceError(f"{self.label} 没有返回可用数据")
        return out


def load_custom() -> list[dict]:
    """Enabled custom sources, as raw config dicts."""
    from kestrel.db.engine import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM data_source WHERE enabled=1 AND kind='custom' ORDER BY priority"
        ).fetchall()
    finally:
        conn.close()

    out = []
    for row in rows:
        item = dict(row)
        try:
            cfg = json.loads(item["config"]) if item["config"] else {}
        except (TypeError, ValueError):
            cfg = {}
        cfg.setdefault("url", "")
        cfg["key"] = item["code"]
        cfg["label"] = item["name"]
        out.append(cfg)
    return out


def save(payload: dict) -> str:
    """Create or update a custom source."""
    from kestrel.db.engine import connect

    key = str(payload.get("key") or "").strip()
    label = str(payload.get("label") or "").strip()
    url = str(payload.get("url") or "").strip()
    if not label:
        raise ValueError("名称不能为空")
    if not url.startswith("http"):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    if not key:
        slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower())[:20]
        key = f"custom_{slug or 'source'}"

    config = {
        "url": url,
        "markets": str(payload.get("markets") or "").strip(),
        "price_path": str(payload.get("price_path") or "price").strip(),
        "prev_close_path": str(payload.get("prev_close_path") or "").strip(),
        "change_path": str(payload.get("change_path") or "").strip(),
        "name_path": str(payload.get("name_path") or "").strip(),
        "currency": str(payload.get("currency") or "").strip(),
        "timeout": payload.get("timeout") or 8,
        "scale": payload.get("scale") or 1,
        "api_key": str(payload.get("api_key") or "").strip(),
        "method": str(payload.get("method") or "GET").upper(),
        "body": str(payload.get("body") or "").strip(),
        "headers": payload.get("headers") or {},
    }

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO data_source (code, name, kind, priority, enabled, config)
            VALUES (?,?,'custom',?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name, config=excluded.config,
                enabled=excluded.enabled
            """,
            (
                key,
                label,
                int(payload.get("priority") or 10),
                int(bool(payload.get("enabled", True))),
                json.dumps(config, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return key


def delete(key: str) -> bool:
    from kestrel.db.engine import connect

    conn = connect()
    try:
        cur = conn.execute("DELETE FROM data_source WHERE code=? AND kind='custom'", (key,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


async def try_source(config: dict, market: str, code: str) -> dict:
    """One-shot request used by the settings page's 'test' button."""
    provider = CustomProvider(config)
    try:
        quotes = await provider.fetch([(market.upper(), code)])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
    quote = quotes.get(f"{market.upper()}:{code}")
    if quote is None:
        return {"ok": False, "error": "拿到了响应，但没解析出价格。检查字段路径。"}
    return {
        "ok": True,
        "price": str(quote.price),
        "prev_close": str(quote.prev_close) if quote.prev_close else None,
        "change_pct": quote.change_pct,
        "name": quote.name,
    }
