"""成交记账：买入、FIFO 卖出、整手校验、偏差计算。

这些数字是产品的核心承诺——「计划和实际的差距算得出来」，
所以每个分支都要有测试钉住。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.services import execution


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def _fill(**over) -> execution.Fill:
    base = {
        "market": "HK",
        "code": "00700",
        "side": "buy",
        "qty": 100,
        "price": Decimal("400"),
        "name": "腾讯控股",
    }
    base.update(over)
    return execution.Fill(**base)


def test_buy_creates_an_open_trade(home):
    result = execution.record(_fill(stop=Decimal("380"), target=Decimal("440")))
    assert result["ok"] is True
    assert result["side"] == "buy"

    conn = connect()
    try:
        row = conn.execute("SELECT * FROM trade WHERE id=?", (result["trade_id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "open"
    assert row["qty"] == 100
    assert row["entry_price"] == 400_000_000
    assert row["stop_price"] == 380_000_000
    assert row["target_price"] == 440_000_000


def test_a_lot_size_mistake_is_refused_for_a_shares(home):
    """A 股永远整手；港股碎股是合法的，不在这里拦。"""
    result = execution.record(
        _fill(market="CN", code="600519", qty=150, price=Decimal("1450"), name="贵州茅台")
    )
    assert result["ok"] is False
    assert "每手 100 股" in result["error"]


def test_hk_odd_lot_buy_is_accepted(home):
    """星财富 / 富途可以买港股碎股，台账必须记得下 150 股这种成交。"""
    result = execution.record(_fill(qty=150))
    assert result["ok"] is True
    conn = connect()
    try:
        row = conn.execute("SELECT qty FROM trade WHERE id=?", (result["trade_id"],)).fetchone()
    finally:
        conn.close()
    assert row["qty"] == 150


def test_fractional_us_shares_are_recorded_as_is(home):
    """美股碎股 0.5 股不能被 int() 截成 0。"""
    result = execution.record(
        _fill(market="US", code="AAPL", qty=Decimal("0.5"), price=Decimal("342.37"), name="苹果")
    )
    assert result["ok"] is True, result.get("error")
    conn = connect()
    try:
        row = conn.execute("SELECT qty FROM trade WHERE id=?", (result["trade_id"],)).fetchone()
    finally:
        conn.close()
    assert Decimal(str(row["qty"])) == Decimal("0.5")


def test_selling_more_than_held_is_refused(home):
    execution.record(_fill())
    result = execution.record(_fill(side="sell", qty=200, price=Decimal("410")))
    assert result["ok"] is False
    assert "超过现有持仓" in result["error"]


def test_selling_without_a_position_is_refused(home):
    result = execution.record(_fill(side="sell", qty=100))
    assert result["ok"] is False
    assert "没有" in result["error"]


def test_partial_sell_keeps_history_honest(home):
    execution.record(_fill())
    result = execution.record(_fill(side="sell", qty=40, price=Decimal("420")))
    assert result["ok"] is True
    assert len(result["closed_trades"]) == 1

    conn = connect()
    try:
        open_rows = conn.execute("SELECT * FROM trade WHERE status='open'").fetchall()
        closed_rows = conn.execute("SELECT * FROM trade WHERE status='closed'").fetchall()
    finally:
        conn.close()
    # 原始建仓数量不被改写：60 股还开着，40 股单独成一条已平记录。
    assert len(open_rows) == 1
    assert open_rows[0]["qty"] == 60
    assert open_rows[0]["entry_price"] == 400_000_000
    assert len(closed_rows) == 1
    assert closed_rows[0]["qty"] == 40
    assert closed_rows[0]["exit_price"] == 420_000_000


def test_sell_fills_the_oldest_position_first(home):
    execution.record(_fill(qty=100, price=Decimal("400"), filled_at="2026-01-02T00:00:00+00:00"))
    execution.record(_fill(qty=100, price=Decimal("410"), filled_at="2026-01-03T00:00:00+00:00"))

    execution.record(
        _fill(side="sell", qty=100, price=Decimal("420"), filled_at="2026-01-04T00:00:00+00:00")
    )
    conn = connect()
    try:
        open_row = conn.execute("SELECT * FROM trade WHERE status='open'").fetchone()
        closed = conn.execute("SELECT * FROM trade WHERE status='closed'").fetchone()
    finally:
        conn.close()
    assert open_row["entry_price"] == 410_000_000
    assert closed["entry_price"] == 400_000_000


def test_deviation_is_measured_against_the_plan(home):
    from kestrel.core.money import to_micro

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size)"
            " VALUES ('HK','00700','腾讯控股','HKD',100)"
        )
        symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        cur = conn.execute(
            """
            INSERT INTO recommendation
                (symbol_id, market, code, name, currency, strategy, score,
                 entry_low, entry_high, stop, target1, qty, lot_size,
                 risk_amount, est_fees, breakeven, r_multiple, confidence,
                 valid_until, invalidation)
            VALUES (?, 'HK','00700','腾讯控股','HKD','x',0.7, ?,?,?,?,100,100,0,0,0,2.0,0.7, ?, '[]')
            """,
            (
                symbol_id, to_micro("400"), to_micro("404"), to_micro("380"),
                to_micro("440"), "2030-01-01T00:00:00+00:00",
            ),
        )
        rec_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    result = execution.record(_fill(price=Decimal("402"), rec_id=rec_id))
    assert result["ok"] is True
    # 买贵了 0.5%，相当于 0.1R（风险距离 20）
    assert result["deviation_pct"] == pytest.approx(0.5)
    assert result["deviation_r"] == pytest.approx(0.1)

    conn = connect()
    try:
        status = conn.execute(
            "SELECT status FROM recommendation WHERE id=?", (rec_id,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "executed"


def test_zero_or_negative_values_are_refused(home):
    assert execution.record(_fill(qty=0))["ok"] is False
    assert execution.record(_fill(price=Decimal("0")))["ok"] is False


def test_paper_adapter_uses_the_cached_quote(home):
    from kestrel.core.money import to_micro

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size)"
            " VALUES ('HK','00700','腾讯控股','HKD',100)"
        )
        symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO quote_latest (symbol_id, price, source, quoted_at)"
            " VALUES (?,?,?,datetime('now'))",
            (symbol_id, to_micro("402.5"), "test"),
        )
        conn.commit()
    finally:
        conn.close()

    result = execution.adapter("paper").execute(_fill(price=Decimal("1")))
    assert result["ok"] is True
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM trade WHERE id=?", (result["trade_id"],)).fetchone()
    finally:
        conn.close()
    assert row["entry_price"] == 402_500_000
    assert row["source"] == "paper"


def test_manual_adapter_is_the_default(home):
    assert execution.adapter(None).code == "manual"
    assert execution.adapter("nope").code == "manual"
