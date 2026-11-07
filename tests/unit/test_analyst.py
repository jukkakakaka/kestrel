"""The analyst's job is to reject: a model's numbers are never trusted."""

from __future__ import annotations

import pytest

from kestrel.services import analyst


def _brief(candidates=None):
    return {
        "account": {"capital_cny": 10000, "risk_per_trade_pct": 2.0, "max_per_sector": 2},
        "candidates": candidates
        or [
            {"market": "US", "code": "AAPL", "name": "苹果", "price": 100.0, "sector": "信息技术"},
            {"market": "US", "code": "MSFT", "name": "微软", "price": 200.0, "sector": "信息技术"},
        ],
    }


def _pick(**over):
    base = {
        "market": "US", "code": "AAPL", "action": "buy", "conviction": 60,
        "thesis": "现价 100 附近，距 20 日线 -1%，缩量回踩",
        "entry_low": 99.7, "entry_high": 100.3, "stop": 95.0, "target": 110.0,
        "invalidation": "收盘跌破 20 日线",
    }
    base.update(over)
    return base


def test_a_sane_pick_passes():
    pick, reason = analyst.validate_pick(_pick(), _brief())
    assert pick is not None, reason
    assert pick.code == "AAPL"
    assert pick.conviction == 60


def test_a_ticker_outside_the_brief_is_rejected():
    pick, reason = analyst.validate_pick(_pick(code="TSLA"), _brief())
    assert pick is None
    assert "不在候选列表" in reason


def test_stop_above_entry_is_rejected():
    pick, reason = analyst.validate_pick(_pick(stop=101.0), _brief())
    assert pick is None
    assert "止损" in reason


def test_stop_too_tight_is_rejected():
    pick, reason = analyst.validate_pick(_pick(stop=99.0), _brief())
    assert pick is None
    assert "止损距离" in reason


def test_stop_too_wide_is_rejected():
    pick, reason = analyst.validate_pick(_pick(stop=80.0), _brief())
    assert pick is None
    assert "止损距离" in reason


def test_reward_below_the_floor_is_rejected():
    # risk 4.7, reward 5.0 -> 1.06R
    pick, reason = analyst.validate_pick(_pick(stop=95.0, target=104.7), _brief())
    assert pick is None
    assert "盈亏比" in reason


def test_entry_far_from_market_is_rejected():
    pick, reason = analyst.validate_pick(
        _pick(entry_low=70.0, entry_high=71.0, stop=60.0, target=95.0), _brief()
    )
    assert pick is None
    assert "15%" in reason


def test_inverted_entry_band_is_rejected():
    pick, reason = analyst.validate_pick(_pick(entry_low=101.0, entry_high=99.0), _brief())
    assert pick is None
    assert "颠倒" in reason


def test_empty_thesis_is_rejected():
    pick, reason = analyst.validate_pick(_pick(thesis="买"), _brief())
    assert pick is None
    assert "理由" in reason


def test_non_numeric_prices_are_rejected_not_crashed():
    pick, reason = analyst.validate_pick(_pick(stop="abc"), _brief())
    assert pick is None
    assert "数字" in reason


def test_conviction_is_clamped():
    pick, _ = analyst.validate_pick(_pick(conviction=250), _brief())
    assert pick is not None
    assert pick.conviction == 100


def test_position_size_comes_from_the_risk_budget():
    pick, _ = analyst.validate_pick(_pick(), _brief())
    qty, fees, risk_amount = analyst.size_position(pick, _brief(), lot_size=1)
    # 10000 CNY capital, 2% risk, 4.7 per share -> ~42 shares, capped by nothing
    assert qty > 0
    assert risk_amount <= 10000 / 7.1 * 0.02 * 1.05
    assert fees >= 0


def test_reply_parser_accepts_fenced_json():
    parsed = analyst.parse_reply('```json\n{"picks": []}\n```')
    assert parsed == {"picks": []}


def test_reply_parser_rejects_prose():
    with pytest.raises(RuntimeError):
        analyst.parse_reply("我认为应该买入苹果")


def test_brief_includes_constraints_and_evidence():
    brief = analyst.build_brief(["US"], limit=5)
    assert "constraints" in brief
    assert brief["constraints"]["min_reward_risk"] == analyst.MIN_R
    assert isinstance(brief["rule_evidence"], list)
