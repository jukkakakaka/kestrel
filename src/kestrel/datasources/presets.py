"""Ready-made configurations for the market data APIs people actually use.

Each preset is just a form pre-filled with the provider's real URL shape and
field paths, so a user pastes an API key and is done. Free and paid tiers use the
same endpoints; the key decides what you get.

``verified`` marks presets whose response shape was checked against the live
endpoint. The rest follow published docs and may need a nudge.
"""

from __future__ import annotations

from typing import Any

PRESETS: list[dict[str, Any]] = [
    {
        "key": "yahoo",
        "label": "Yahoo Finance（免费，无需 Key）",
        "markets": "US,HK,CN",
        "url": "https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}?interval=1d&range=1d",
        "price_path": "chart.result[0].meta.regularMarketPrice",
        "prev_close_path": "chart.result[0].meta.chartPreviousClose",
        "name_path": "chart.result[0].meta.shortName",
        "change_path": "",
        "scale": 1,
        "headers": {"User-Agent": "Mozilla/5.0"},
        "note": "延迟约 1 分钟，覆盖美股/港股/A 股。注意：这是非官方接口，可能被限流。",
        "verified": True,
        "docs": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
    },
    {
        "key": "finnhub",
        "label": "Finnhub（免费额度 / 付费实时）",
        "markets": "US",
        "url": "https://finnhub.io/api/v1/quote?symbol={code}&token={key}",
        "price_path": "c",
        "prev_close_path": "pc",
        "change_path": "dp",
        "name_path": "",
        "scale": 1,
        "note": "免费档 60 次/分钟，付费档给美股实时。字段：c=现价 pc=昨收 dp=涨跌%。",
        "verified": False,
        "docs": "https://finnhub.io/docs/api/quote",
    },
    {
        "key": "alphavantage",
        "label": "Alpha Vantage（免费 25 次/天 / 付费）",
        "markets": "US,HK,CN",
        "url": "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={code}&apikey={key}",
        "price_path": '["Global Quote"]["05. price"]',
        "prev_close_path": '["Global Quote"]["08. previous close"]',
        "change_path": '["Global Quote"]["10. change percent"]',
        "name_path": "",
        "scale": 1,
        "note": "键名里带空格和点号，所以路径用方括号包起来。免费额度很小，适合盘后核对。",
        "verified": False,
        "docs": "https://www.alphavantage.co/documentation/#latestprice",
    },
    {
        "key": "twelvedata",
        "label": "Twelve Data（免费 8 次/分钟 / 付费实时）",
        "markets": "US,HK,CN",
        "url": "https://api.twelvedata.com/quote?symbol={code}&apikey={key}",
        "price_path": "close",
        "prev_close_path": "previous_close",
        "change_path": "percent_change",
        "name_path": "name",
        "scale": 1,
        "note": "免费档够用做盘后；付费档有 WebSocket 实时，这里只取快照。",
        "verified": False,
        "docs": "https://twelvedata.com/docs#quote",
    },
    {
        "key": "polygon",
        "label": "Polygon.io（付费实时，美股）",
        "markets": "US",
        "url": "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{code}?apiKey={key}",
        "price_path": "ticker.day.c",
        "prev_close_path": "ticker.prevDay.c",
        "change_path": "ticker.todaysChangePerc",
        "name_path": "",
        "scale": 1,
        "note": "美股实时最稳的一家之一。免费档只有延迟数据且限流。",
        "verified": False,
        "docs": "https://polygon.io/docs/stocks/get_v2_snapshot_locale_us_markets_stocks_tickers__ticker",
    },
    {
        "key": "tiingo",
        "label": "Tiingo（免费 / 付费 IEX 实时）",
        "markets": "US",
        "url": "https://api.tiingo.com/tiingo/daily/{code}/prices?token={key}",
        "price_path": "[0].close",
        "prev_close_path": "[0].prevClose",
        "change_path": "",
        "name_path": "",
        "scale": 1,
        "note": "返回数组，路径用 [0] 取第一条。日线为主，盘中不是逐笔。",
        "verified": False,
        "docs": "https://www.tiingo.com/documentation/end-of-day",
    },
]


def get(key: str) -> dict[str, Any] | None:
    for preset in PRESETS:
        if preset["key"] == key:
            return preset
    return None


def as_form(preset: dict[str, Any]) -> dict[str, Any]:
    """Shape a preset the way the settings form expects it."""
    return {
        "preset": preset["key"],
        "label": preset["label"],
        "markets": preset.get("markets", ""),
        "url": preset.get("url", ""),
        "price_path": preset.get("price_path", ""),
        "prev_close_path": preset.get("prev_close_path", ""),
        "change_path": preset.get("change_path", ""),
        "name_path": preset.get("name_path", ""),
        "scale": preset.get("scale", 1),
        "headers": preset.get("headers") or {},
        "note": preset.get("note", ""),
        "docs": preset.get("docs", ""),
    }
