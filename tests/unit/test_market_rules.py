"""多市场制度校验单元测试：科创板 200+1、A 股 T+1、港股每手、美股点号代码与 Tick 规整。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel.core.money import round_tick
from kestrel.core.quantity import is_star_or_bse, validate_order_qty
from kestrel.datasources.base import default_lot_size, guess_market, normalize_code
from kestrel.db.migrate import init_db
from kestrel.services import execution, exits


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def test_star_and_bse_200_plus_1_rules():
    """科创板 688 与北交所 8/43/920 必须至少买入 200 股，其后支持按 1 股递增。"""
    assert is_star_or_bse("CN", "688981") is True
    assert is_star_or_bse("CN", "600519") is False

    ok_low, msg_low = validate_order_qty("CN", "688981", Decimal("100"), 100, side="buy")
    assert ok_low is False and "200" in msg_low

    ok_205, _ = validate_order_qty("CN", "688981", Decimal("205"), 100, side="buy")
    assert ok_205 is True


def test_cn_t_plus_1_blocks_same_day_sell_and_allows_next_day(home):
    """A 股当日买入同日禁止卖出，跨日后可正常卖出，补录历史底仓可同日卖出。"""
    buy_res = execution.record(
        execution.Fill(
            market="CN",
            code="600519",
            side="buy",
            qty=100,
            price=Decimal("1500"),
            stop=Decimal("1400"),
            target=Decimal("1700"),
            filled_at="2026-10-12T10:00:00+08:00",
            name="贵州茅台",
        )
    )
    assert buy_res["ok"] is True

    # 同日卖出被 T+1 拦截
    same_day = execution.record(
        execution.Fill(
            market="CN",
            code="600519",
            side="sell",
            qty=100,
            price=Decimal("1520"),
            filled_at="2026-10-12T14:30:00+08:00",
        )
    )
    assert same_day["ok"] is False
    assert "T+1" in same_day["error"]

    # T+1 锁定期内的 exits 退出建议也应返回 t1_hold 而非 exit_all
    plan = exits.suggest(
        {
            "id": buy_res["trade_id"],
            "market": "CN",
            "code": "600519",
            "currency": "CNY",
            "qty": 100,
            "entry_price": 1500_000_000,
            "stop_price": 1400_000_000,
            "initial_stop": 1400_000_000,
            "target_price": 1700_000_000,
        },
        {"price": Decimal("1380"), "can_sell_today": False},
    )
    assert plan is not None
    assert plan.action == "t1_hold"

    # 次日可正常卖出
    next_day = execution.record(
        execution.Fill(
            market="CN",
            code="600519",
            side="sell",
            qty=100,
            price=Decimal("1520"),
            filled_at="2026-10-13T09:35:00+08:00",
        )
    )
    assert next_day["ok"] is True


def test_cn_one_lot_at_target_does_not_suggest_invalid_half_sell(home):
    """A 股持仓仅 1 手（100 股）到达目标价时，绝不误建议卖出 50 股零股。"""
    plan = exits.suggest(
        {
            "id": 9,
            "market": "CN",
            "code": "600519",
            "currency": "CNY",
            "qty": 100,
            "entry_price": 1500_000_000,
            "stop_price": 1400_000_000,
            "initial_stop": 1400_000_000,
            "target_price": 1700_000_000,
        },
        {"price": Decimal("1710"), "can_sell_today": True},
    )
    assert plan is not None
    assert plan.action == "take_partial"
    assert plan.qty == Decimal("100")


def test_hk_lot_overrides_dotted_us_ticker_and_tick_rounding():
    """验证港股差异每手、美股 BRK.B 代码识别、与港股阶梯 Tick 规整。"""
    assert default_lot_size("HK", "00005") == 400
    assert default_lot_size("HK", "09988") == 100
    assert guess_market("BRK.B") == "US"
    assert normalize_code("US", "brk.b") == "BRK.B"

    # 港股 20–100 港元区间 Tick 为 0.05，412.23 -> 412.20（200-500 区间 Tick 为 0.20）
    assert round_tick("52.34", "HK", "00005") == Decimal("52.35")
    assert round_tick("412.14", "HK", "00700") == Decimal("412.20")
    assert round_tick("1.2349", "CN", "510300") == Decimal("1.235")
