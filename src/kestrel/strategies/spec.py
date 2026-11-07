"""Rules as data.

A strategy used to be a Python class, which meant only someone who could edit
this repository could add one. A rule is now a small JSON document: a list of
conditions that must all hold, bonuses that raise the score, and where to put the
stop and target. Anyone can add one from the UI, and every rule — built-in or
user-made — is measured by the same evaluator.

The language is deliberately narrow. It compares indicators with numbers, because
that is what can be validated. Anything richer belongs in code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kestrel.strategies.base import StrategyResult

OPS = {">", ">=", "<", "<=", "between", "is_true", "is_false", "cross_above", "cross_below"}

# Indicators a rule may reference. Keeping the list closed means a typo in a
# user rule fails loudly at save time instead of silently never triggering.
FIELDS = {
    "last", "sma5", "sma10", "sma20", "sma60", "ema12", "ema26", "macd",
    "rsi14", "atr14", "atr_pct", "vol_ratio", "high20", "low20", "high60",
    "dist_sma20", "dist_sma60", "dist_high20", "range_pos20",
    "ret5", "ret20", "ret60", "close_pos", "slope20", "ma_stack", "trend_up",
}


class RuleError(ValueError):
    """A rule that cannot be compiled; the message is shown to the author."""


def _value(token: Any, ind: dict) -> float | None:
    """Resolve a token: a number, a field name, or a percentage of price."""
    if isinstance(token, (int, float)):
        return float(token)
    if isinstance(token, str):
        name = token.strip()
        if name not in FIELDS:
            raise RuleError(f"未知指标「{name}」")
        raw = ind.get(name)
        return float(raw) if isinstance(raw, (int, float)) else None
    return None


@dataclass(slots=True)
class Condition:
    left: str
    op: str
    right: Any = None
    low: float | None = None
    high: float | None = None

    @classmethod
    def parse(cls, spec: dict) -> Condition:
        left = str(spec.get("left") or "").strip()
        if left not in FIELDS:
            raise RuleError(f"条件里的指标「{left}」不存在")
        op = str(spec.get("op") or "").strip()
        if op not in OPS:
            raise RuleError(f"不支持的操作符「{op}」")

        cond = cls(left=left, op=op)
        if op == "between":
            span = spec.get("value")
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                raise RuleError("between 需要 [下限, 上限]")
            cond.low, cond.high = float(span[0]), float(span[1])
        elif op in ("is_true", "is_false"):
            pass
        else:
            if "right" not in spec:
                raise RuleError(f"「{op}」需要一个 right 值")
            cond.right = spec["right"]
        return cond

    def test(self, ind: dict, prev: dict | None = None) -> bool:
        left = ind.get(self.left)
        if left is None:
            return False
        if self.op == "is_true":
            return bool(left)
        if self.op == "is_false":
            return not bool(left)
        if self.op == "between":
            return self.low <= float(left) <= self.high

        right = _value(self.right, ind)
        if right is None:
            return False
        left = float(left)
        if self.op == ">":
            return left > right
        if self.op == ">=":
            return left >= right
        if self.op == "<":
            return left < right
        if self.op == "<=":
            return left <= right
        if self.op in ("cross_above", "cross_below"):
            if prev is None:
                return False
            was = prev.get(self.left)
            if was is None:
                return False
            was, now = float(was), float(left)
            threshold_now = _value(self.right, ind)
            threshold_was = _value(self.right, prev)
            if threshold_now is None or threshold_was is None:
                return False
            if self.op == "cross_above":
                return was <= threshold_was and now > threshold_now
            return was >= threshold_was and now < threshold_now
        return False


@dataclass(slots=True)
class Rule:
    """A compiled, runnable strategy built from a JSON document."""

    code: str
    name: str
    spec: dict
    conditions: list[Condition] = field(default_factory=list)
    bonuses: list[tuple[Condition, float]] = field(default_factory=list)
    base_score: float = 0.4
    stop_field: str | None = None
    target_field: str | None = None

    def evaluate(self, ind: dict) -> StrategyResult | None:
        if not ind:
            return None
        for cond in self.conditions:
            if not cond.test(ind):
                return None

        score = self.base_score
        hits: list[str] = []
        for cond, add in self.bonuses:
            if cond.test(ind):
                score += add
                hits.append(f"{cond.left}{cond.op}{cond.right or ''}")

        score = max(0.0, min(1.0, score))
        stop = ind.get(self.stop_field) if self.stop_field else None
        target = ind.get(self.target_field) if self.target_field else None
        return StrategyResult(
            code=self.code,
            score=score,
            reasons=hits or [c.left for c in self.conditions[:3]],
            stop_hint=float(stop) if isinstance(stop, (int, float)) else None,
            target_hint=float(target) if isinstance(target, (int, float)) else None,
        )


def compile_rule(spec: dict) -> Rule:
    """Validate and compile. Raises :class:`RuleError` with a readable message."""
    if not isinstance(spec, dict):
        raise RuleError("规则必须是一个 JSON 对象")

    code = str(spec.get("code") or "").strip()
    name = str(spec.get("name") or "").strip()
    if not code or not code.replace("_", "").isalnum():
        raise RuleError("code 只能用字母、数字和下划线")
    if not name:
        raise RuleError("名称不能为空")

    raw_conditions = spec.get("conditions")
    if not isinstance(raw_conditions, list) or not raw_conditions:
        raise RuleError("至少要有一个进场条件（conditions）")

    conditions = [Condition.parse(c) for c in raw_conditions if isinstance(c, dict)]
    if not conditions:
        raise RuleError("条件格式不对")

    score_spec = spec.get("score") or {}
    bonuses: list[tuple[Condition, float]] = []
    for bonus in score_spec.get("bonus") or []:
        if not isinstance(bonus, dict):
            continue
        cond = Condition.parse(bonus.get("when") or {})
        bonuses.append((cond, float(bonus.get("add") or 0)))

    stop_field = (spec.get("stop") or {}).get("field")
    target_field = (spec.get("target") or {}).get("field")
    for label, token in (("止损", stop_field), ("目标", target_field)):
        if token and token not in FIELDS:
            raise RuleError(f"{label}引用了不存在的指标「{token}」")

    return Rule(
        code=code,
        name=name,
        spec=spec,
        conditions=conditions,
        bonuses=bonuses,
        base_score=float(score_spec.get("base") or 0.4),
        stop_field=stop_field,
        target_field=target_field,
    )


def from_json(text: str) -> Rule:
    try:
        spec = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"不是合法 JSON：{exc}") from exc
    return compile_rule(spec)


def describe(rule: Rule) -> str:
    """A sentence a person can read, generated from the rule itself."""
    parts = []
    for cond in rule.conditions:
        if cond.op == "between":
            parts.append(f"{cond.left} 在 {cond.low}~{cond.high} 之间")
        elif cond.op in ("is_true", "is_false"):
            parts.append(f"{cond.left} 为 {'真' if cond.op == 'is_true' else '假'}")
        else:
            parts.append(f"{cond.left} {cond.op} {cond.right}")
    return "，且".join(parts)
