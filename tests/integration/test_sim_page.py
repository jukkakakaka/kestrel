"""The quick-simulation page has to render its empty and refusal states, and it
must accept any symbol — not just the handful already in the pool.

The resolver is patched here so the test never touches the network: what matters
is that an unknown ticker produces a readable refusal, and that a ticker outside
the cache is accepted and stored.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.app import create_app
from kestrel.datasources import resolve as resolver
from kestrel.datasources.base import normalize_code
from kestrel.db.migrate import init_db
from kestrel.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    with TestClient(create_app()) as test_client:
        yield test_client


def _found(market: str, code: str, name: str):
    class _Found:
        def __init__(self) -> None:
            self.market = market
            self.code = normalize_code(market, code)
            self.name = name
            self.exchange = market
            self.secid = ""
            self.currency = {"CN": "CNY", "HK": "HKD"}.get(market, "USD")
            self.lot_size = 100 if market in ("CN", "HK") else 1

    return _Found()


def test_sim_page_renders(client):
    response = client.get("/sim")
    assert response.status_code == 200
    assert "快速模拟" in response.text
    # the horizon control has to be there, or weekly backtests are unreachable
    assert 'name="horizon"' in response.text


def test_sim_accepts_a_free_text_symbol(client):
    """输入框而不是下拉框：任意代码都能填。"""
    page = client.get("/sim")
    assert 'name="symbol" list="sim-symbols"' in page.text


def test_sim_without_symbol_refuses_instead_of_crashing(client):
    response = client.post("/sim", data={"symbol": "", "strategy": "trend_pullback"})
    assert response.status_code == 200
    assert "先填一个股票代码" in response.text


def test_sim_with_unknown_symbol_refuses_instead_of_crashing(client, monkeypatch):
    async def _missing(code, market=None):
        return None

    monkeypatch.setattr(resolver, "resolve", _missing)
    response = client.post(
        "/sim",
        data={"symbol": "US/ZZZZ", "strategy": "trend_pullback", "horizon": "medium"},
    )
    assert response.status_code == 200
    assert "找不到" in response.text


def test_a_symbol_outside_the_cache_is_resolved_and_stored(client, monkeypatch):
    async def _ok(code, market=None):
        return _found("US", code, "测试标的")

    monkeypatch.setattr(resolver, "resolve", _ok)
    response = client.post("/sim", data={"symbol": "AAPL", "strategy": "trend_pullback"})
    assert response.status_code == 200

    from kestrel.db.engine import connect

    conn = connect()
    try:
        row = conn.execute(
            "SELECT name FROM symbol WHERE market='US' AND code='AAPL'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["name"] == "测试标的"


def test_every_rule_is_offered_as_a_strategy(client):
    """策略下拉要列出全部规则，而不是写死三条。"""
    page = client.get("/sim")
    for name in ("趋势回踩", "20 日突破", "超跌反弹", "缩量回踩", "温和动量", "低波趋势", "超跌长下影"):
        assert name in page.text, name
