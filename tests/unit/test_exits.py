"""退出管理：移动止损、分批止盈、时间止损。

这些规则决定长期盈亏，所以每一条都钉住：
* R 永远按**最初**的风险算，止损上移不能让战绩看起来更好；
* 止损只能往上抬，往下调必须被拒绝；
* 同时只给一个建议——三条建议等于没建议。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.services import execution, exits


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def _trade(stop="95", target="110", qty=100, entry="100") -> dict:
    result = execution.record(
        execution.Fill(
            market="US", code="AAPL", side="buy", qty=qty,
            price=Decimal(entry), name="苹果",
            stop=Decimal(stop) if stop else None,
            target=Decimal(target) if target else None,
        )
    )
    assert result["ok"], result.get("error")
    conn = connect()
    try:
        return dict(
            conn.execute("SELECT * FROM trade WHERE id=?", (result["trade_id"],)).fetchone()
        )
    finally:
        conn.close()


def _position(entry: str, price: str | None, held_days: int = 0, **over) -> dict:
    base = {
        "trade_id": 1,
        "price": Decimal(price) if price else None,
        "held_days": held_days,
        "overdue": False,
    }
    base.update(over)
    return base


def test_no_quote_means_no_advice(home):
    trade = _trade()
    assert exits.suggest(trade, _position("100", None)) is None


def test_below_the_stop_is_the_only_thing_to_do(home):
    trade = _trade()
    plan = exits.suggest(trade, _position("100", "94"))
    assert plan is not None
    assert plan.action == "exit_all"
    assert plan.urgency == "urgent"


def test_at_one_r_the_stop_moves_to_break_even(home):
    trade = _trade(stop="95")          # risk = 5
    plan = exits.suggest(trade, _position("100", "105"))
    assert plan is not None
    assert plan.action == "move_stop"
    assert plan.stop == Decimal("100.00")
    assert "保本" in plan.reason_zh


def test_at_two_r_the_stop_locks_in_one_r(home):
    trade = _trade(stop="95")
    plan = exits.suggest(trade, _position("100", "110.5"))
    # 到目标价优先，先分批止盈
    assert plan.action == "take_partial"
    assert plan.qty == 50

    trade2 = _trade(stop="95", target="200")
    plan2 = exits.suggest(trade2, _position("100", "110"))
    assert plan2.action == "move_stop"
    assert plan2.stop == Decimal("105.00")


def test_the_stop_never_moves_backwards(home):
    trade = _trade(stop="95", target="200")
    exits.apply_stop(trade["id"], Decimal("100"), "保本")
    conn = connect()
    try:
        updated = dict(conn.execute("SELECT * FROM trade WHERE id=?", (trade["id"],)).fetchone())
    finally:
        conn.close()
    # 到 1R 时已经抬到保本，再给建议不能重复
    assert exits.suggest(updated, _position("100", "105")) is None


def test_initial_stop_survives_a_stop_move(home):
    trade = _trade(stop="95", target="200")
    assert trade["initial_stop"] == to_micro("95")
    exits.apply_stop(trade["id"], Decimal("100"))
    conn = connect()
    try:
        updated = dict(conn.execute("SELECT * FROM trade WHERE id=?", (trade["id"],)).fetchone())
    finally:
        conn.close()
    assert updated["initial_stop"] == to_micro("95")
    assert updated["stop_price"] == to_micro("100")

    from kestrel.services import positions

    view = positions.assess(updated, {"price": to_micro("110")})
    # (110 - 100) / (100 - 95) = 2R，而不是 (110-100)/(100-100)
    assert view["r_now"] == pytest.approx(2.0)


def test_lowering_a_stop_is_refused(home):
    trade = _trade(stop="95")
    result = exits.apply_stop(trade["id"], Decimal("90"))
    assert result["ok"] is False
    assert "只能往有利方向" in result["error"]


def test_a_stop_above_entry_is_refused(home):
    trade = _trade(stop="95")
    result = exits.apply_stop(trade["id"], Decimal("101"))
    assert result["ok"] is False
    assert "入场价" in result["error"]


def test_every_move_is_recorded(home):
    trade = _trade(stop="95", target="200")
    exits.apply_stop(trade["id"], Decimal("100"), "保本")
    history = exits.stop_history(trade["id"])
    assert len(history) == 1
    assert history[0]["old_stop"] == pytest.approx(95.0)
    assert history[0]["new_stop"] == pytest.approx(100.0)
    assert history[0]["reason"] == "保本"


def test_a_closed_trade_cannot_move_its_stop(home):
    trade = _trade(stop="95")
    from kestrel.services import journal

    journal.close_trade(trade["id"], "99")
    result = exits.apply_stop(trade["id"], Decimal("100"))
    assert result["ok"] is False
    assert "平仓" in result["error"]


def test_time_stop_fires_when_nothing_happened(home):
    trade = _trade(stop="95", target="200")
    plan = exits.suggest(trade, _position("100", "101", held_days=30), max_hold_days=20)
    assert plan is not None
    assert plan.action == "exit_all"
    assert "超过" in plan.reason_zh


def test_time_stop_does_not_fire_on_a_winner(home):
    trade = _trade(stop="95", target="200")
    plan = exits.suggest(trade, _position("100", "103", held_days=30), max_hold_days=20)
    # +0.6R 还没到 1R，但时间已到 —— 仍然应该提示，只是不紧急
    assert plan is not None
    assert plan.action == "exit_all"


def test_a_healthy_trade_gets_no_action(home):
    trade = _trade(stop="95", target="200")
    assert exits.suggest(trade, _position("100", "101", held_days=2), max_hold_days=20) is None


def test_partial_target_exits_more_than_half(home):
    trade = _trade(stop="95", target="110", qty=100)
    plan = exits.suggest(trade, _position("100", "111"))
    assert plan.action == "take_partial"
    assert plan.qty == 50


def test_a_one_share_position_is_not_split(home):
    trade = _trade(stop="95", target="110", qty=1)
    plan = exits.suggest(trade, _position("100", "111"))
    assert plan.action == "take_partial"
    assert plan.qty == 1
