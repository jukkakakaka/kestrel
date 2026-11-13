"""记一笔成交：最少字段、自动补齐、碎股与小数股。

这些用例覆盖用户实际会怎么填——不是所有字段都填，而是「只填我知道的」：
代码或名称、方向、数量、价格。其余由程序推出来。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.fees import plans as plan_service
from kestrel.services import glossary, rules
from kestrel.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    plan_service.hydrate()
    glossary.seed()
    rules.ensure_seeded()
    from kestrel.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _seed_symbol(market: str, code: str, name: str, lot: int = 100) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
            " VALUES (?,?,?,?,?)",
            (market, code, name, {"CN": "CNY", "HK": "HKD"}.get(market, "USD"), lot),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_fill_needs_only_code_qty_and_price(client):
    """市场、名称、手续费都不填也要成。"""
    response = client.post(
        "/api/journal/fill",
        json={"code": "AAPL", "qty": "1", "price": "200", "side": "buy"},
    )
    body = response.json()
    assert body["ok"] is True, body.get("error")

    conn = connect()
    try:
        row = conn.execute("SELECT * FROM trade").fetchone()
    finally:
        conn.close()
    assert row["market"] == "US"
    assert row["name"] == "AAPL"
    assert row["fees"] > 0, "手续费应按券商套餐自动算出来"


def test_the_market_comes_from_the_code_shape(client):
    response = client.post(
        "/api/journal/fill",
        json={"code": "00700", "qty": "100", "price": "400"},
    )
    assert response.json()["ok"] is True
    conn = connect()
    try:
        row = conn.execute("SELECT market FROM trade").fetchone()
    finally:
        conn.close()
    assert row["market"] == "HK"


def test_a_name_from_the_database_resolves_the_ticker(client):
    _seed_symbol("HK", "00700", "腾讯控股")
    response = client.post(
        "/api/journal/fill",
        json={"code": "腾讯控股", "qty": "100", "price": "400"},
    )
    body = response.json()
    assert body["ok"] is True, body.get("error")
    conn = connect()
    try:
        row = conn.execute("SELECT market, code FROM trade").fetchone()
    finally:
        conn.close()
    assert (row["market"], row["code"]) == ("HK", "00700")


def test_an_unresolvable_name_is_refused_with_advice(client):
    response = client.post(
        "/api/journal/fill",
        json={"code": "不存在的公司", "qty": "100", "price": "10"},
    )
    assert response.status_code == 400
    assert "找不到" in response.json()["error"]


def test_a_stop_can_be_left_empty(client):
    """止损留空不是错误：持仓页会少一条风险提示，但账还是记下了。"""
    response = client.post(
        "/api/journal/fill",
        json={"code": "AAPL", "qty": "1", "price": "200"},
    )
    assert response.json()["ok"] is True
    conn = connect()
    try:
        row = conn.execute("SELECT stop_price FROM trade").fetchone()
    finally:
        conn.close()
    assert row["stop_price"] is None


def test_fractional_us_shares_survive_the_round_trip(client):
    response = client.post(
        "/api/journal/fill",
        json={"code": "AAPL", "qty": "0.5", "price": "200"},
    )
    assert response.json()["ok"] is True
    conn = connect()
    try:
        row = conn.execute("SELECT qty FROM trade").fetchone()
    finally:
        conn.close()
    assert float(row["qty"]) == 0.5


def test_hk_odd_lot_is_recorded(client):
    response = client.post(
        "/api/journal/fill",
        json={"code": "00700", "qty": "150", "price": "400"},
    )
    assert response.json()["ok"] is True
    conn = connect()
    try:
        row = conn.execute("SELECT qty FROM trade").fetchone()
    finally:
        conn.close()
    assert row["qty"] == 150


def test_a_share_lot_mistake_is_still_refused(client):
    _seed_symbol("CN", "600519", "贵州茅台")
    response = client.post(
        "/api/journal/fill",
        json={"code": "600519", "qty": "150", "price": "1450"},
    )
    body = response.json()
    assert body["ok"] is False
    assert "每手" in body["error"]


def test_the_suggest_endpoint_fills_in_the_rest(client):
    _seed_symbol("HK", "00700", "腾讯控股")
    body = client.get(
        "/api/journal/fill/suggest",
        params={"code": "00700", "price": "400", "qty": "100"},
    ).json()
    assert body["market"] == "HK"
    assert body["name"] == "腾讯控股"
    assert body["known"] is True
    assert body["fees"] > 0
    # 没有缓存 K 线时不给瞎编的止损
    assert body["levels"]["ok"] is False
    assert body["levels"]["reason"]
