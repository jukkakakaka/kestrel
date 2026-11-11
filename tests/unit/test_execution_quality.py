"""执行质量：计划 vs 实际。

产品承诺里最容易落空的一句是「陪你把纪律练出来」。要让这句话成立，
必须能回答两个问题：建议被执行了吗？成交价偏离计划多少？
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.services import execution


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def _plan(entry="100", stop="95", qty=100, market="US", code="AAPL",
          status="skipped") -> int:
    conn = connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO symbol (market, code, name, currency, lot_size)"
            " VALUES (?,?,?,?,1)",
            (market, code, "苹果", "USD"),
        )
        symbol_id = conn.execute(
            "SELECT id FROM symbol WHERE market=? AND code=?", (market, code)
        ).fetchone()["id"]
        cur = conn.execute(
            """
            INSERT INTO recommendation
                (symbol_id, market, code, name, currency, strategy, score,
                 entry_low, entry_high, stop, target1, qty, lot_size,
                 risk_amount, est_fees, breakeven, r_multiple, confidence,
                 valid_until, invalidation, status)
            VALUES (?,?,?,'苹果','USD','x',0.7, ?,?,?,?,?,1,0,0,0,2.0,0.7,
                    '2030-01-01T00:00:00+00:00','[]',?)
            """,
            (
                symbol_id, market, code, to_micro(entry), to_micro(entry),
                to_micro(stop), to_micro("110"), qty, status,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _buy(rec_id: int, price: str, qty: int = 100) -> None:
    result = execution.record(
        execution.Fill(
            market="US", code="AAPL", side="buy", qty=qty,
            price=Decimal(price), rec_id=rec_id, name="苹果",
        )
    )
    assert result["ok"], result.get("error")


def test_no_history_says_so_instead_of_showing_zeros(home):
    report = execution.quality_report()
    assert report["advice_total"] == 0
    assert report["follow_rate"] is None
    assert "还没有执行过" in execution.lesson(report)


def test_follow_rate_counts_unfollowed_advice_in_the_denominator(home):
    rec_id = _plan()
    _plan(code="MSFT")
    _buy(rec_id, "100")
    report = execution.quality_report()
    assert report["executed"] == 1
    assert report["advice_total"] == 2
    assert report["follow_rate"] == pytest.approx(0.5)


def test_deviation_is_reported_in_percent_and_r(home):
    rec_id = _plan()
    _buy(rec_id, "101")
    report = execution.quality_report()
    assert report["avg_deviation_pct"] == pytest.approx(1.0)
    # 风险距离是 5，多付 1 就是 0.2R
    assert report["avg_deviation_r"] == pytest.approx(0.2)
    assert "买贵" in execution.lesson(report)


def test_a_cheaper_fill_gets_a_different_warning(home):
    rec_id = _plan()
    _buy(rec_id, "98")
    report = execution.quality_report()
    assert report["avg_deviation_r"] == pytest.approx(-0.4)
    assert "便宜" in execution.lesson(report)


def test_partial_fills_are_counted(home):
    rec_id = _plan(qty=100)
    _buy(rec_id, "100", qty=40)
    report = execution.quality_report()
    assert report["partial_fills"] == 1
    assert "一部分" in execution.lesson(report)


def test_best_and_worst_are_named(home):
    first = _plan(code="AAPL")
    second = _plan(code="MSFT")
    _buy(first, "99")
    _buy(second, "102")
    report = execution.quality_report()
    assert report["worst"]["code"] == "MSFT"
    assert report["best"]["code"] == "AAPL"
