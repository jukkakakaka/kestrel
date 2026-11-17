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
