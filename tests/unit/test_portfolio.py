"""动态资金账本与资产/持仓手动校准单元测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel.db.migrate import init_db
from kestrel.services import execution, portfolio


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def test_cash_updates_dynamically_with_buys_and_sells(home):
    """买入按成本+买费扣减可用现金，分批与全平正确回笼现金并计入净盈亏。"""
    s0 = portfolio.snapshot(include_quotes=False)
    assert s0["initial_capital_cny"] == pytest.approx(50000.0)
    assert s0["available_cash_cny"] == pytest.approx(50000.0)

    # 在港股买入 100 股 @ 100 HKD + 50 HKD 费用（汇率 0.92 -> cost = 10050 * 0.92 = 9246 CNY）
    buy = execution.record(
        execution.Fill(
            market="HK",
            code="00700",
            side="buy",
            qty=100,
            price=Decimal("100"),
            fees=Decimal("50"),
            name="腾讯控股",
        )
    )
    assert buy["ok"] is True

    s1 = portfolio.snapshot(include_quotes=False)
    assert s1["available_cash_cny"] == pytest.approx(50000.0 - 10050.0 * 0.92, rel=1e-4)
    assert s1["market_value_cny"] == pytest.approx(10000.0 * 0.92, rel=1e-4)
    # 含买费浮亏 -50 HKD * 0.92 = -46 CNY
    assert s1["total_equity_cny"] == pytest.approx(50000.0 - 50.0 * 0.92, rel=1e-4)

    # 分批卖出 40 股 @ 120 HKD，卖出费用 30 HKD：分摊买费 20 + 卖费 30 = 50 HKD
    # 毛利 = (120 - 100) * 40 = 800 HKD，净利 = 750 HKD -> 690 CNY
    sell1 = execution.record(
        execution.Fill(
            market="HK",
            code="00700",
            side="sell",
            qty=40,
            price=Decimal("120"),
            fees=Decimal("30"),
        )
    )
    assert sell1["ok"] is True
    s2 = portfolio.snapshot(include_quotes=False)
    # 剩余 60 股成本 6000 + 分摊买费 30 = 6030 HKD；回笼资金 = 4800 - 30 = 4770 HKD
    expected_cash = 50000.0 - 10050.0 * 0.92 + (4800.0 - 30.0) * 0.92
    assert s2["available_cash_cny"] == pytest.approx(expected_cash, rel=1e-4)
    assert s2["realized_pnl_cny"] == pytest.approx(750.0 * 0.92, rel=1e-4)


def test_calibrate_cash_available_equity_and_deposits(home):
    """支持直接对齐可用现金、总资产、银证转入转出和修改初始本金。"""
    res1 = portfolio.calibrate_cash(delta_cny=10000, kind="deposit", note="银证转入")
    assert res1["ok"] is True
    assert res1["snapshot"]["available_cash_cny"] == pytest.approx(60000.0)
    assert res1["snapshot"]["contributed_cny"] == pytest.approx(60000.0)

    res2 = portfolio.calibrate_cash(target_available_cny=58500, note="对账修正")
    assert res2["ok"] is True
    assert res2["snapshot"]["available_cash_cny"] == pytest.approx(58500.0)

    res3 = portfolio.calibrate_cash(target_equity_cny=62000, note="对齐总资产")
    assert res3["ok"] is True
    assert res3["snapshot"]["total_equity_cny"] == pytest.approx(62000.0)


def test_calibrate_and_backfill_existing_positions(home):
    """补录已有股票底仓默认不影响可用现金；修改与清零均可正常对冲。"""
    # 默认不扣现金补录 2 股 AAPL @ 200 USD（2 * 200 * 7.1 = 2840 CNY）
    add_res = portfolio.calibrate_position(
        market="US",
        code="AAPL",
        qty=2,
        entry_price=200,
        stop_price=180,
        target_price=240,
        affect_cash=False,
    )
    assert add_res["ok"] is True
    snap1 = add_res["snapshot"]
    assert snap1["open_count"] == 1
    assert snap1["available_cash_cny"] == pytest.approx(50000.0, rel=1e-4)
    assert snap1["market_value_cny"] == pytest.approx(2840.0, rel=1e-4)
    assert snap1["total_equity_cny"] == pytest.approx(52840.0, rel=1e-4)

    # 校准为 3 股，并测试清零删除
    tid = add_res["trade_id"]
    upd_res = portfolio.calibrate_position(trade_id=tid, qty=3, entry_price=205)
    assert upd_res["ok"] is True
    assert upd_res["snapshot"]["positions"][0]["qty_num"] == 3.0

    del_res = portfolio.calibrate_position(trade_id=tid, qty=0, entry_price=205)
    assert del_res["ok"] is True
    assert del_res["snapshot"]["open_count"] == 0
    assert del_res["snapshot"]["available_cash_cny"] == pytest.approx(50000.0, rel=1e-4)
