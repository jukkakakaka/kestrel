"""One measurement, one set of numbers.

``services/edge.py`` used to carry a second backtest with its own thresholds and
a gate nothing called. These tests pin what replaced it: the per-fold metrics in
``rules`` and the report shape the API promises.
"""

from __future__ import annotations

import pytest

from kestrel.services import edge, rules


def _trades(rows: list[tuple[str, float]]) -> list[dict]:
    return [{"date": date, "index": index, "r": r} for index, (date, r) in enumerate(rows)]


def test_expectancy_is_the_mean_r():
    stats = rules._stats(_trades([("2026-01-01", 2.0), ("2026-01-02", -1.0)] * 20))
    assert stats["n"] == 40
    assert stats["win"] == pytest.approx(50.0)
    assert stats["exp"] == pytest.approx(0.5)


def test_profit_factor_and_losing_streak_are_measured():
    stats = rules._stats(
        _trades(
            [
                ("2026-01-01", 2.0),
                ("2026-01-02", -1.0),
                ("2026-01-03", -1.0),
                ("2026-01-04", -1.0),
            ]
        )
    )
    assert stats["pf"] == pytest.approx(0.67, abs=0.01)
    assert stats["streak"] == 3


def test_drawdown_follows_entry_date_not_pool_order():
    # The same three trades: chronological order never dips more than 1R, but
    # the order the scanner happens to return symbols in dips 2R. Sorting by
    # date is what makes the number mean something.
    rows = [("2026-01-02", 2.0), ("2026-01-01", -1.0), ("2026-01-03", -1.0)]
    assert rules._stats(_trades(rows))["dd"] == pytest.approx(-1.0)


def test_empty_fold_reports_zeros_not_an_error():
    stats = rules._stats([])
    assert stats["n"] == 0
    assert stats["pf"] == 0.0
    assert stats["streak"] == 0


def test_thin_sample_never_overwrites_a_real_verdict():
    assert rules._should_record("无优势", persist=True) is True
    assert rules._should_record("样本不足", persist=True) is False
    assert rules._should_record("有优势", persist=False) is False


def test_edge_report_maps_the_rule_results(monkeypatch):
    sample = [
        {
            "ok": True,
            "code": "trend_pullback",
            "timeframe": "medium",
            "verdict": "无优势",
            "avg_exp": 0.12,
            "avg_edge": -0.03,
            "folds": [{"n": 10}, {"n": 12}, {"n": 8}],
            "trades": 30,
        },
        {"ok": False, "code": "broken", "error": "没有足够的历史数据"},
    ]
    monkeypatch.setattr(rules, "validate_all", lambda **kwargs: sample)

    class _Conn:
        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(edge, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(edge.config_service, "put", lambda *a, **k: None)

    report = edge.evaluate(max_symbols=50, horizon="medium")
    assert report["ok"] is True
    assert report["horizon"] == "medium"
    assert report["strategies"]["trend_pullback"]["verdict"] == "无优势"
    assert report["strategies"]["trend_pullback"]["trades"] == 30
    assert report["strategies"]["broken"]["verdict"] == "failed"
