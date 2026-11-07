"""Expectancy maths and the strategy gate.

The gate exists because a rule that backtests as a loser must not produce
advice. These tests pin the arithmetic that decides it.
"""

from __future__ import annotations

import pytest

from kestrel.services import edge


def _trades(results: list[float]) -> list[dict]:
    return [{"r": r} for r in results]


def test_expectancy_is_the_mean_r():
    agg = edge._aggregate(_trades([2.0, -1.0, 2.0, -1.0]))
    assert agg["trades"] == 4
    assert agg["win_rate"] == pytest.approx(0.5)
    assert agg["expectancy"] == pytest.approx(0.5)


def test_losing_series_is_not_usable():
    agg = edge._aggregate(_trades([-1.0] * 40 + [2.0] * 10))
    assert agg["expectancy"] < 0
    agg["verdict"] = edge._verdict(agg)
    assert agg["verdict"] == "losing"
    assert not edge._usable(agg)


def test_small_sample_is_insufficient_not_good():
    agg = edge._aggregate(_trades([2.0, 2.0, 2.0]))
    agg["verdict"] = edge._verdict(agg)
    assert agg["verdict"] == "insufficient"
    assert not edge._usable(agg)


def test_positive_series_is_usable():
    agg = edge._aggregate(_trades([2.0, -1.0] * 40))
    agg["verdict"] = edge._verdict(agg)
    assert agg["verdict"] == "positive"
    assert edge._usable(agg)


def test_loss_streak_and_drawdown_are_measured():
    agg = edge._aggregate(_trades([2.0, -1.0, -1.0, -1.0, -1.0, 2.0]))
    assert agg["max_loss_streak"] == 4
    assert agg["max_drawdown_r"] == pytest.approx(-4.0)


def test_profit_factor_handles_no_losses():
    agg = edge._aggregate(_trades([2.0, 2.0]))
    assert agg["profit_factor"] >= 99.0


def test_describe_speaks_plainly():
    agg = edge._aggregate(_trades([2.0, -1.0] * 30))
    text = edge.describe(agg)
    assert "R" in text and "胜率" in text
    losing = edge._aggregate(_trades([-1.0] * 40))
    assert "不该用" in edge.describe(losing)
