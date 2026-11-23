from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from kestrel.core.clock import market_tz
from kestrel.datasources import history


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("market", "code", "yahoo_code"),
    [("CN", "600519", "600519.SS"), ("HK", "00700", "0700.HK"), ("US", "BRK.B", "BRK-B")],
)
async def test_daily_fallback_preserves_market_dates_and_filters_bad_candles(
    market, code, yahoo_code, monkeypatch,
):
    from kestrel.services import calendar

    monkeypatch.setattr(calendar, "last_closed_session", lambda _: "2026-11-20")
    zone = market_tz(market)
    timestamps = [int(datetime(2026, 11, day, 9, 30, tzinfo=zone).timestamp()) for day in (18, 19, 20)]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "push2his.eastmoney.com":
            raise httpx.RemoteProtocolError("daily feed unavailable", request=request)
        assert request.url.path.endswith("/" + yahoo_code)
        assert request.url.params["interval"] == "1d"
        return httpx.Response(200, json={"chart": {"result": [{
            "timestamp": timestamps,
            "indicators": {"quote": [{
                "open": [9.0, None, 10.0], "high": [11.0, None, 12.0],
                "low": [8.0, None, 9.0], "close": [10.0, None, 11.1], "volume": [100, None, 150],
            }]},
        }]}})

    client_cls = httpx.AsyncClient
    monkeypatch.setattr(history.httpx, "AsyncClient", lambda **kw: client_cls(
        transport=httpx.MockTransport(handle), **kw,
    ))
    bars = await history.fetch_daily(market, code, limit=3)
    assert [row["date"].isoformat() for row in bars] == ["2026-11-18", "2026-11-20"]
    assert bars[-1]["close"] == Decimal("11.1")
    assert {bar["source"] for bar in bars} == {"yahoo"}


def test_switching_feeds_replaces_the_old_price_series(tmp_path):
    from kestrel.db.engine import connect
    from kestrel.db.migrate import migrate
    from kestrel.services.bars import cached_bars, save_bars

    path = tmp_path / "prices.db"
    migrate(path)
    conn = connect(path)
    try:
        symbol_id = conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size) VALUES ('US','AAPL','Apple','USD',1)"
        ).lastrowid
        bar = {"date": "2026-11-19", "open": 10, "high": 11, "low": 9,
               "close": 10, "volume": 100, "turnover": 0}
        save_bars(conn, symbol_id, [bar], depth=40)
        save_bars(conn, symbol_id, [bar | {"date": "2026-11-20", "source": "yahoo"}], depth=30)
        assert [r["date"] for r in cached_bars(conn, symbol_id)] == ["2026-11-20"]
        assert tuple(conn.execute("SELECT bars_source,bars_depth FROM symbol").fetchone()) == ("yahoo", 30)
    finally:
        conn.close()
