"""持仓盯盘与今日待办。

两条规则值得钉住：
1. 没有现价时，宁可显示「不知道」也不显示 0 —— 0 会被当成真实盈亏；
2. 同一种提醒对同一笔持仓只发一次，否则每 15 分钟响一次，用户就会关掉通知。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.services import execution, positions, today


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def _buy(stop="380", target="440", qty=100, price="400") -> int:
    result = execution.record(
        execution.Fill(
            market="HK",
            code="00700",
            side="buy",
            qty=qty,
            price=Decimal(price),
            name="腾讯控股",
            stop=Decimal(stop) if stop else None,
            target=Decimal(target) if target else None,
        )
    )
    assert result["ok"], result.get("error")
    return int(result["trade_id"])


def _quote(price: str) -> None:
    conn = connect()
    try:
        symbol_id = conn.execute(
            "SELECT id FROM symbol WHERE market='HK' AND code='00700'"
        ).fetchone()["id"]
        conn.execute(
            """INSERT INTO quote_latest (symbol_id, price, change_pct, source, quoted_at)
               VALUES (?,?,?,?,datetime('now'))
               ON CONFLICT(symbol_id) DO UPDATE SET
                 price=excluded.price, change_pct=excluded.change_pct,
                 fetched_at=datetime('now')""",
            (symbol_id, to_micro(price), 0.0, "test"),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_position_without_a_quote_reports_unknown_not_zero(home):
    _buy()
    view = positions.open_positions()[0]
    assert view["price"] is None
    assert view["pnl"] is None
    assert view["state"] == "no_quote"


def test_float_pnl_and_distance_to_stop_use_the_live_price(home):
    _buy()
    _quote("410")
    view = positions.open_positions()[0]
    assert view["price"] == Decimal("410")
    assert view["pnl"] == pytest.approx(1000.0)
    assert view["pnl_pct"] == pytest.approx(2.5)
    assert view["to_stop_pct"] == pytest.approx((410 - 380) / 410 * 100)
    assert view["r_now"] == pytest.approx(0.5)


def test_states_cover_every_actionable_case(home):
    _buy()
    for price, expected in [
        ("375", "stopped"),
        ("385", "near_stop"),
        ("441", "target"),
        ("434", "near_target"),
        ("410", "ok"),
    ]:
        _quote(price)
        assert positions.open_positions()[0]["state"] == expected, price


def test_summary_puts_the_closest_stop_first(home):
    _buy()
    execution.record(
        execution.Fill(
            market="HK", code="00700", side="buy", qty=100,
            price=Decimal("400"), name="腾讯控股", stop=Decimal("390"),
        )
    )
    _quote("395")
    book = positions.summary(positions.open_positions())
    assert book["count"] == 2
    # 距止损 1.27% 的那笔应该排前面，而不是按 id
    assert book["risky"][0]["stop"] == Decimal("390")


def test_an_alert_is_sent_once_per_position_and_kind(home):
    _buy()
    _quote("385")
    first = positions.sweep_alerts()
    assert [a["kind"] for a in first] == ["near_stop"]
    assert positions.sweep_alerts() == []


def test_a_new_kind_of_alert_is_still_allowed(home):
    _buy()
    _quote("385")
    positions.sweep_alerts()
    _quote("375")
    second = positions.sweep_alerts()
    assert [a["kind"] for a in second] == ["stop_hit"]


def test_today_briefing_leads_with_the_urgent_thing(home):
    _buy()
    _quote("375")
    brief = today.briefing()
    assert brief["urgent"] >= 1
    assert brief["todos"][0]["kind"] == today.URGENT
    assert brief["book"]["count"] == 1


def test_today_briefing_says_nothing_to_do_when_nothing_is_wrong(home):
    _buy()
    _quote("410")
    brief = today.briefing()
    assert brief["urgent"] == 0
    assert all(item["kind"] != today.URGENT for item in brief["todos"])


def test_today_briefing_survives_an_empty_account(home):
    brief = today.briefing()
    assert brief["book"]["count"] == 0
    # 空账户时应该引导去扫描，而不是显示一片空白
    assert any(item["tag"] == "开始" for item in brief["todos"])
