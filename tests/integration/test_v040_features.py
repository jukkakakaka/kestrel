"""v0.4.0 核心端到端集成测试：动态资金校准、学习模式切换与右侧炒股 Agent 接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.db.migrate import init_db
from kestrel.fees import plans as plan_service
from kestrel.services import config as config_service
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


def test_portfolio_calibration_api_roundtrip(client):
    """前端资金概览、手动校准现金与补录已有底仓全链路可用。"""
    p0 = client.get("/api/portfolio").json()
    assert p0["ok"] is True
    assert p0["available_cash_cny"] == pytest.approx(50000.0)

    # 校准现金至 80000
    c1 = client.post(
        "/api/portfolio/calibrate-cash",
        json={"target_available_cny": 80000, "note": "对账一致"},
    ).json()
    assert c1["ok"] is True
    assert c1["snapshot"]["available_cash_cny"] == pytest.approx(80000.0)

    # 补录已有持仓 10 股 AAPL @ 150 USD（不扣现金）
    c2 = client.post(
        "/api/portfolio/calibrate-position",
        json={
            "market": "US",
            "code": "AAPL",
            "qty": "10",
            "entry_price": "150",
            "stop_price": "135",
            "affect_cash": False,
        },
    ).json()
    assert c2["ok"] is True
    assert c2["snapshot"]["open_count"] == 1
    assert c2["snapshot"]["available_cash_cny"] == pytest.approx(80000.0)

    # 在实时预演接口上测算买入与卖出预期
    prev = client.post(
        "/api/journal/fill/preview",
        json={"market": "US", "code": "MSFT", "side": "buy", "qty": "5", "price": "400"},
    ).json()
    assert prev["ok"] is True
    assert prev["est_fees"] > 0


def test_learning_toggle_and_glossary_api_behavior(client):
    """学习模式开启返回 75+ 词条与自动匹配表；关闭时 glossary API 返回停用且无打扰。"""
    assert client.get("/api/glossary").json()["enabled"] is False
    assert client.post("/api/ui/learning", json={"enabled": True}).json()["enabled"]
    on_data = client.get("/api/glossary").json()
    assert on_data["enabled"] is True
    assert len(on_data["items"]) >= 70
    assert len(on_data["matchers"]) >= 70

    off_res = client.post("/api/ui/learning", json={"enabled": False}).json()
    assert off_res["ok"] is True and off_res["enabled"] is False

    off_data = client.get("/api/glossary").json()
    assert off_data["enabled"] is False
    assert off_data["items"] == {}

    # 首页与建议页在关闭学习模式时应标记 data-learning="0"
    from kestrel.db.engine import connect

    conn = connect()
    try:
        config_service.put(conn, "ui.onboarded", True)
        conn.commit()
    finally:
        conn.close()

    home_html = client.get("/").text
    assert 'data-learning="0"' in home_html
    assert client.post("/api/settings", json={"items": {"ui.glossary": False}}).json()["ok"]
    assert client.post("/api/ui/learning", json={"enabled": True}).json()["enabled"]
    assert 'data-learning="1"' in client.get("/sim").text
    assert client.get("/api/glossary").json()["enabled"] is False


def test_copilot_endpoints_and_scope_guard(client, monkeypatch):
    """右侧炒股 Agent 接口：未配置前拒答、快捷配置后可聊天且拦截无关话题。"""
    st0 = client.get("/api/copilot/status").json()
    assert st0["configured"] is False

    bad_unconfigured = client.post("/api/copilot/chat", json={"message": "看下我仓位"})
    assert bad_unconfigured.status_code == 400
    assert bad_unconfigured.json()["code"] == "not_configured"

    cfg = client.post(
        "/api/copilot/config",
        json={"base_url": "https://api.example.com/v1", "model": "deepseek-chat", "api_key": "sk-demo"},
    ).json()
    assert cfg["ok"] is True and cfg["status"]["configured"] is True

    # 超范围话题被本地守卫拦截
    rejected = client.post("/api/copilot/chat", json={"message": "教我做西红柿炒鸡蛋"}).json()
    assert rejected["ok"] is True and rejected["rejected"] is True

    # 合法话题调用 LLM
    from kestrel.services import copilot

    class _FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"choices": [{"message": {"content": "当前总资产 50000，风险可控。"}}]}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return _FakeResp()

    monkeypatch.setattr(copilot.httpx, "Client", _FakeClient)

    ok_chat = client.post("/api/copilot/chat", json={"message": "我当前仓位安全吗？"}).json()
    assert ok_chat["ok"] is True and ok_chat["rejected"] is False
    assert "50000" in ok_chat["reply"]
