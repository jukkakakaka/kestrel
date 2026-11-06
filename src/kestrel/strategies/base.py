"""Strategy contract and the built-in rule set.

Each strategy is a pure function of the indicator snapshot, returning a score in
``0..1`` plus machine readable reason codes. No strategy allocates position size —
that belongs to :mod:`kestrel.planning`.
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

    def evaluate(self, ind: dict) -> StrategyResult | None: ...


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class TrendPullback:
    """Uptrend intact, price has pulled back to the 20-day average."""

    code = "trend_pullback"

    def evaluate(self, ind: dict) -> StrategyResult | None:
        if not ind or ind.get("sma20") is None or ind.get("sma60") is None:
            return None
        if not ind["trend_up"]:
            return None
        dist = ind.get("dist_sma20")
        rsi = ind.get("rsi14")
        if dist is None or rsi is None or not (-4.0 <= dist <= 1.5):
            return None
        if not (32 <= rsi <= 62):
            return None

        proximity = 1 - abs(dist) / 4.0
        trend = _clamp(ind.get("slope20", 0) / 2.0)
        score = 0.45 + 0.35 * proximity + 0.20 * trend
        return StrategyResult(
            code=self.code,
            score=_clamp(score),
            reasons=["trend_up", "pullback_to_ma20", "rsi_neutral"],
            stop_hint=ind.get("low20"),
            target_hint=ind.get("high20"),
        )


class Breakout20:
    """Close above the prior 20-day high on expanding volume."""

    code = "breakout_20"

    def evaluate(self, ind: dict) -> StrategyResult | None:
        if not ind or ind.get("high20") is None:
            return None
        last = ind["last"]
        if last <= ind["high20"]:
            return None
        vol_ratio = ind.get("vol_ratio") or 0
        if vol_ratio < 1.2:
            return None
        atr_pct = ind.get("atr_pct") or 99
        if atr_pct > 9:
            return None

        strength = _clamp((vol_ratio - 1.2) / 1.2)
        score = 0.5 + 0.3 * strength + 0.2 * _clamp(ind.get("slope20", 0) / 2.0)
        return StrategyResult(
            code=self.code,
            score=_clamp(score),
            reasons=["breakout_20d", "volume_expansion"],
            stop_hint=ind["high20"],
            target_hint=None,
        )


class MeanReversion:
    """Oversold bounce candidate — only inside a longer-term uptrend."""

    code = "mean_reversion"

    def evaluate(self, ind: dict) -> StrategyResult | None:
        if not ind or ind.get("rsi14") is None or ind.get("sma60") is None:
            return None
        rsi = ind["rsi14"]
        last = ind["last"]
        if rsi > 33:
            return None
        if last < ind["sma60"] * 0.94:
            return None
        if (ind.get("dist_sma20") or 0) > -3:
            return None

        depth = _clamp((33 - rsi) / 20)
        score = 0.4 + 0.45 * depth
        return StrategyResult(
            code=self.code,
            score=_clamp(score),
            reasons=["rsi_oversold", "above_sma60"],
            stop_hint=ind.get("low20"),
            target_hint=ind.get("sma20"),
        )


BUILTIN: list[Strategy] = [TrendPullback(), Breakout20(), MeanReversion()]


def registry() -> dict[str, Strategy]:
    return {s.code: s for s in BUILTIN}
