"""策略基础契约与默认编译注册表。

策略以 JSON 规则形式存储在 `strategies/library.py` 与数据库 `strategy_rule` 表中，
此处不再保留硬编码的 Python 策略类，避免两套实现互相漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class StrategyResult:
    code: str
    score: float
    reasons: list[str] = field(default_factory=list)
    stop_hint: float | None = None
    target_hint: float | None = None


class Strategy(Protocol):
    code: str

    def evaluate(self, ind: dict, prev: dict | None = None) -> StrategyResult | None: ...


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def registry() -> dict[str, Strategy]:
    """返回基于内置规则库编译后的默认策略映射表。"""
    from kestrel.strategies import library
    from kestrel.strategies.spec import compile_rule

    compiled: dict[str, Strategy] = {}
    for item in library.RULES:
        rule = compile_rule(item["spec"])
        compiled[rule.code] = rule
    return compiled
