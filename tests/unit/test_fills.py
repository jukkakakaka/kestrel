"""记一笔成交时，用户没填的东西应该由程序补，而不是让用户去查。

三条规则：
1. 市场从代码形状推（6 位=A 股、≤5 位=港股、字母=美股），库里有的以库为准；
2. 名称优先用库里/解析出的，不要求用户再填一遍；
3. 手续费按券商套餐算、止损目标按 K 线波动给建议——都是可覆盖的建议值，
   算不出来就明说，不编数字。
"""

from __future__ import annotations

import pytest

from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.fees import plans as plan_service
from kestrel.services import fills


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    plan_service.hydrate()
    return tmp_path


def _seed_bars(market: str, code: str, prices: list[float]) -> None:
    from datetime import date, timedelta

    from kestrel.services import bars as bars_service

    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
            " VALUES (?,?,?,?,100)",
            (market, code, f"TEST {code}", "HKD" if market == "HK" else "USD"),
        )
        symbol_id = conn.execute(
            "SELECT id FROM symbol WHERE market=? AND code=?", (market, code)
        ).fetchone()["id"]
        start = date(2024, 1, 1) - timedelta(days=len(prices))
        rows = [
            {
                "date": str(start + timedelta(days=i)),
                "open": p * 0.998, "high": p * 1.01, "low": p * 0.99,
                "close": p, "volume": 1_000_000, "turnover": p * 1_000_000,
            }
            for i, p in enumerate(prices)
        ]
        bars_service.save_bars(conn, symbol_id, rows, depth=len(rows))
        conn.commit()
    finally:
        conn.close()


def test_market_is_inferred_from_the_code_shape(home):
    assert fills.resolve_target("00700").market == "HK"
    assert fills.resolve_target("600519").market == "CN"
    assert fills.resolve_target("AAPL").market == "US"


def test_an_explicit_market_wins(home):
    target = fills.resolve_target("00700", "US")
    assert target.market == "US"


def test_a_known_symbol_supplies_name_and_lot_size(home):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size)"
            " VALUES ('HK','00700','腾讯控股','HKD',100)"
        )
        conn.commit()
    finally:
        conn.close()

    target = fills.resolve_target("00700")
    assert target.known is True
    assert target.name == "腾讯控股"
    assert target.lot_size == 100

    # 只打名称也能找到，且不会把港股认成美股。
    by_name = fills.resolve_target("腾讯控股")
    assert by_name.market == "HK"
    assert by_name.code == "00700"


def test_an_unknown_name_is_not_guessed_into_a_market(home):
    target = fills.resolve_target("不存在的名字")
    assert target.market == ""
    assert target.known is False


def test_fees_follow_the_broker_plan(home):
    cheap = fills.estimate_fees("HK", "400", "100", "fosun_hk")
    pricey = fills.estimate_fees("HK", "400", "100", "za_hk")
    assert cheap > 0 and pricey > 0
    assert pricey > cheap, "a 0.05% commission plan must cost more than a flat 15 HKD one"


def test_fees_are_zero_when_the_notional_is_missing(home):
    assert fills.estimate_fees("HK", "", "", "fosun_hk") == 0


def test_levels_come_from_cached_bars_and_explain_themselves(home):
    _seed_bars("HK", "00005", [100 + i * 0.2 for i in range(120)])
    result = fills.suggest_levels("HK", "00005", "124")
    assert result["ok"] is True
    assert result["stop"] < 124 < result["target"]
    assert result["risk_pct"] > 0
    assert "ATR" in result["basis"]


def test_levels_say_so_when_there_is_not_enough_history(home):
    result = fills.suggest_levels("HK", "99999", "10")
    assert result["ok"] is False
    assert "K 线" in result["reason"]


def test_levels_are_not_invented_without_a_price(home):
    result = fills.suggest_levels("HK", "00005", "")
    assert result["ok"] is False
    assert "价格" in result["reason"]
