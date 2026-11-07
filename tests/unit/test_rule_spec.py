"""The rule language: compiling, rejecting bad rules, and evaluating them."""

from __future__ import annotations

import pytest

from kestrel.strategies.spec import RuleError, compile_rule, describe, from_json

GOOD = {
    "code": "test_rule",
    "name": "测试规则",
    "conditions": [
        {"left": "ma_stack", "op": "is_true"},
        {"left": "dist_sma20", "op": "between", "value": [-2, 0.5]},
        {"left": "last", "op": ">", "right": "sma60"},
    ],
    "score": {
        "base": 0.4,
        "bonus": [{"when": {"left": "vol_ratio", "op": "<", "right": 1.1}, "add": 0.2}],
    },
    "stop": {"field": "low20"},
    "target": {"field": "high20"},
}


def _ind(**over):
    base = {
        "last": 100.0, "sma20": 99.0, "sma60": 95.0, "ma_stack": True,
        "dist_sma20": -1.0, "rsi14": 50.0, "vol_ratio": 1.0,
        "low20": 94.0, "high20": 108.0, "atr_pct": 2.0,
    }
    base.update(over)
    return base


def test_compiles_and_fires_when_every_condition_holds():
    rule = compile_rule(GOOD)
    result = rule.evaluate(_ind())
    assert result is not None
    assert result.code == "test_rule"
    assert result.stop_hint == 94.0
    assert result.target_hint == 108.0
    assert 0.4 <= result.score <= 1.0


def test_one_failed_condition_blocks_the_signal():
    rule = compile_rule(GOOD)
    assert rule.evaluate(_ind(ma_stack=False)) is None
    assert rule.evaluate(_ind(dist_sma20=5.0)) is None


def test_bonus_raises_the_score_but_does_not_gate():
    rule = compile_rule(GOOD)
    plain = rule.evaluate(_ind(vol_ratio=1.4))
    boosted = rule.evaluate(_ind(vol_ratio=0.9))
    assert plain is not None and boosted is not None
    assert boosted.score > plain.score


def test_unknown_indicator_is_rejected_with_a_readable_message():
    bad = dict(GOOD, conditions=[{"left": "not_a_field", "op": ">", "right": 1}])
    with pytest.raises(RuleError) as err:
        compile_rule(bad)
    assert "not_a_field" in str(err.value)


def test_unknown_operator_is_rejected():
    bad = dict(GOOD, conditions=[{"left": "rsi14", "op": "approximately", "right": 50}])
    with pytest.raises(RuleError):
        compile_rule(bad)


def test_between_needs_two_bounds():
    bad = dict(GOOD, conditions=[{"left": "rsi14", "op": "between", "value": [50]}])
    with pytest.raises(RuleError):
        compile_rule(bad)


def test_a_rule_without_conditions_is_rejected():
    with pytest.raises(RuleError):
        compile_rule({"code": "x", "name": "x", "conditions": []})


def test_code_must_be_identifier_like():
    with pytest.raises(RuleError):
        compile_rule(dict(GOOD, code="我的 规则"))


def test_missing_indicator_value_means_no_signal_not_a_crash():
    rule = compile_rule(GOOD)
    assert rule.evaluate(_ind(dist_sma20=None)) is None


def test_from_json_reports_syntax_errors():
    with pytest.raises(RuleError) as err:
        from_json("{not json")
    assert "JSON" in str(err.value)


def test_describe_reads_like_a_sentence():
    text = describe(compile_rule(GOOD))
    assert "dist_sma20" in text and "之间" in text
