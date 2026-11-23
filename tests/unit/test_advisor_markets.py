"""多市场智能推荐、快进快出周期与冷启动评估测试。"""

from __future__ import annotations

import pytest

from kestrel.db.migrate import init_db
from kestrel.services import advisor, timeframe


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def test_fast_horizon_exists_and_is_valid():
    """快进快出（fast）周期已被纳入完整配置与选项。"""
    assert timeframe.is_valid("fast") is True
    cfg = timeframe.get("fast")
    assert cfg.max_hold_days <= 10
    codes = [item["code"] for item in timeframe.options("zh-CN")]
    assert codes[0] == "fast"


def test_market_recommendations_scale_with_capital(home):
    """不同初始资金应给出合乎手续费和整手门槛的市场推荐与理由。"""
    small = advisor.recommend_markets(capital_cny=8000)
    assert small["recommended_markets"] == ["US"]
    assert "美股" in small["primary_zh"]
    assert len(small["reasons_zh"]) >= 2

    mid = advisor.recommend_markets(capital_cny=50000)
    assert set(mid["recommended_markets"]) == {"CN", "US"}

    large = advisor.recommend_markets(capital_cny=300000)
    assert set(large["recommended_markets"]) == {"CN", "HK", "US"}


def test_cold_start_evaluation_is_not_locked_as_fresh_cache(home):
    """没有任何 K 线时冷启动评估可正常返回推荐并保留理由，且不会死锁后续有数据重算。"""
    rep = advisor.evaluate(force=True)
    assert rep["horizon"] in ("fast", "short", "medium", "long")
    assert rep["cold_start"] is True
    assert rep["reason"]


def test_market_preview_uses_same_horizon_model_as_live_configuration(home):
    from kestrel.db.engine import connect
    from kestrel.services import config

    simulated = advisor.recommend_markets(capital_cny=8000, enabled=["HK"], configured=True)
    conn = connect()
    try:
        config.put(conn, "account.capital", 8000)
        config.put(conn, "scan.markets", ["HK"])
    finally:
        conn.close()
    live = advisor.evaluate()
    assert live["metrics"]["markets"] == ["HK"]
    assert simulated["recommended_horizon"] == live["horizon"]
    assert live["primary_reason"] in simulated["reasons_zh"]


@pytest.mark.asyncio
async def test_scan_reports_old_bars_and_never_plans_from_them(home, monkeypatch):
    from kestrel.db.engine import connect
    from kestrel.services import config, scan

    conn = connect()
    try:
        config.put(conn, "scan.markets", ["HK"])
        config.put(conn, "scan.universe", "watchlist")
        config.put(conn, "trade.horizon", "short")
    finally:
        conn.close()

    ticker = {"id": 1, "market": "HK", "code": "00700", "name": "Tencent", "secid": "116.00700"}
    monkeypatch.setattr(scan, "_pick_symbols", lambda *_: [ticker])

    async def old_cache(*_args, **_kwargs):
        return {1: [{"date": "2020-01-03"}]}

    monkeypatch.setattr(scan, "_load_bars", old_cache)
    result = await scan.run_scan()
    assert result["ok"]
    assert result["plans"] == 0
    assert result["skipped"] == [
        {"market": "HK", "code": "00700", "name": "Tencent", "reason": "stale_bars", "bars": 1},
    ]
