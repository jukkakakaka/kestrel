"""Indicator engine — plain Python, no pandas, deterministic."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Series:
    close: list[float]
    high: list[float]
    low: list[float]
    volume: list[float]

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_bars(cls, bars: list[dict]) -> Series:
        return cls(
            close=[float(b["close"]) for b in bars],
            high=[float(b["high"]) for b in bars],
            low=[float(b["low"]) for b in bars],
            volume=[float(b["volume"] or 0) for b in bars],
        )


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    value = sum(values[:n]) / n
    for price in values[n:]:
        value = price * k + value * (1 - k)
    return value


def rsi(values: list[float], n: int = 14) -> float | None:
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / n
    avg_loss = losses / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> float | None:
    if len(close) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / n


def slope(values: list[float], n: int = 20) -> float:
    """Normalised slope of the last ``n`` closes, in percent per bar."""
    if len(values) < n or values[-n] == 0:
        return 0.0
    return (values[-1] - values[-n]) / values[-n] / n * 100


def compute(series: Series) -> dict:
    close = series.close
    if len(close) < 30:
        return {}

    last = close[-1]
    ma5, ma10, ma20, ma60 = (sma(close, n) for n in (5, 10, 20, 60))
    a = atr(series.high, series.low, close, 14)
    r = rsi(close, 14)
    recent_vol = sum(series.volume[-5:]) / 5 if len(series.volume) >= 5 else 0
    base_vol = sum(series.volume[-20:]) / 20 if len(series.volume) >= 20 else 0

    high20 = max(series.high[-21:-1]) if len(series.high) >= 21 else max(series.high)
    low20 = min(series.low[-21:-1]) if len(series.low) >= 21 else min(series.low)
    high60 = max(series.high[-61:-1]) if len(series.high) >= 61 else max(series.high)

    return {
        "last": last,
        "sma5": ma5,
        "sma10": ma10,
        "sma20": ma20,
        "sma60": ma60,
        "ema12": ema(close, 12),
        "ema26": ema(close, 26),
        "rsi14": r,
        "atr14": a,
        "atr_pct": (a / last * 100) if a and last else None,
        "vol_ratio": (recent_vol / base_vol) if base_vol else None,
        "high20": high20,
        "low20": low20,
        "high60": high60,
        "dist_sma20": ((last - ma20) / ma20 * 100) if ma20 else None,
        "dist_sma60": ((last - ma60) / ma60 * 100) if ma60 else None,
        "slope20": slope(close, 20),
        "trend_up": bool(ma20 and ma60 and ma20 > ma60 and last > ma60),
        "bars": len(close),
    }
