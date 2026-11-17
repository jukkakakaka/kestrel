"""技术指标计算引擎 — 纯 Python 实现，确定性无外部依赖。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Series:
    close: list[float]
    high: list[float]
    low: list[float]
    volume: list[float]
    open: list[float] = field(default_factory=list)
    bar_days: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.open:
            self.open = list(self.close)
        if not self.bar_days:
            self.bar_days = [1] * len(self.close)

    def __len__(self) -> int:
        return len(self.close)

    @classmethod
    def from_bars(cls, bars: list[dict]) -> Series:
        return cls(
            close=[float(b["close"]) for b in bars],
            high=[float(b["high"]) for b in bars],
            low=[float(b["low"]) for b in bars],
            volume=[float(b.get("volume") or 0) for b in bars],
            open=[float(b.get("open", b["close"])) for b in bars],
            bar_days=[int(b.get("bar_days") or 1) for b in bars],
        )


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def sma_series(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < n or n <= 0:
        return out
    running = sum(values[:n])
    out[n - 1] = running / n
    for i in range(n, len(values)):
        running += values[i] - values[i - n]
        out[i] = running / n
    return out


def ema(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    value = sum(values[:n]) / n
    for price in values[n:]:
        value = price * k + value * (1 - k)
    return value


def ema_series(values: list[float], n: int) -> list[float]:
    if len(values) < n or n <= 0:
        return []
    k = 2 / (n + 1)
    value = sum(values[:n]) / n
    out = [value]
    for price in values[n:]:
        value = price * k + value * (1 - k)
        out.append(value)
    return out


def rsi(values: list[float], n: int = 14) -> float | None:
    """Wilder 平滑（RMA）计算 RSI，消除简单滑窗的窗口边界跳变效应。"""
    if len(values) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / n
    avg_loss = losses / n
    for i in range(n + 1, len(values)):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> float | None:
    """Wilder 平滑（RMA）计算真实波幅 ATR。"""
    if len(close) < n + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(close)):
        tr = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
        trs.append(tr)
    if len(trs) < n:
        return None
    value = sum(trs[:n]) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def slope(values: list[float], n: int = 20) -> float:
    """计算 n 周期均线近 10 根的归一化斜率（每根均线变动百分比）。"""
    ma = sma_series(values, n)
    valid = [x for x in ma if x is not None]
    look = min(10, len(valid) - 1)
    if look >= 1 and valid[-1 - look]:
        past = valid[-1 - look]
        return (valid[-1] - past) / past / look * 100
    if len(values) < n or values[-n] == 0:
        return 0.0
    return (values[-1] - values[-n]) / values[-n] / n * 100


def _norm_volumes(volumes: list[float], bar_days: list[int]) -> list[float]:
    """将周/月线末尾尚未收盘的 K 线成交量按已完成交易天数做等比例归一化，防止周一虚假缩量。"""
    if len(volumes) < 2:
        return list(volumes)
    typical = max(bar_days[:-1]) if len(bar_days) > 1 else 1
    last_days = bar_days[-1] if bar_days else 1
    out = list(volumes)
    if typical > 1 and 0 < last_days < typical:
        out[-1] = volumes[-1] * (typical / last_days)
    return out


def compute(series: Series) -> dict:
    close = series.close
    if len(close) < 30:
        return {}

    last = close[-1]
    prev_close = close[-2] if len(close) >= 2 else last
    open_last = series.open[-1] if series.open else last

    ma5, ma10, ma20, ma60 = (sma(close, n) for n in (5, 10, 20, 60))
    a = atr(series.high, series.low, close, 14)
    a5 = atr(series.high, series.low, close, 5)
    a20 = atr(series.high, series.low, close, 20)
    r = rsi(close, 14)

    vols = _norm_volumes(series.volume, series.bar_days)
    recent_vol = sum(vols[-5:]) / 5 if len(vols) >= 5 else 0.0
    base_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else 0.0
    today_vol = vols[-1] if vols else 0.0

    high20 = max(series.high[-21:-1]) if len(series.high) >= 21 else max(series.high)
    low20 = min(series.low[-21:-1]) if len(series.low) >= 21 else min(series.low)
    high60 = max(series.high[-61:-1]) if len(series.high) >= 61 else max(series.high)
    low60 = min(series.low[-61:-1]) if len(series.low) >= 61 else min(series.low)

    def ret(days: int) -> float | None:
        if len(close) <= days:
            return None
        past = close[-1 - days]
        return ((last - past) / past * 100) if past else None

    e12 = ema_series(close, 12)
    e26 = ema_series(close, 26)
    macd_dif = None
    macd_sig = None
    macd_hist = None
    if e26:
        offset = len(e12) - len(e26)
        dif_series = [e12[offset + i] - e26[i] for i in range(len(e26))]
        macd_dif = dif_series[-1]
        dea_series = ema_series(dif_series, 9)
        if dea_series:
            macd_sig = dea_series[-1]
            macd_hist = (macd_dif - macd_sig) * 2

    day_range = series.high[-1] - series.low[-1]

    return {
        "last": last,
        "open": open_last,
        "sma5": ma5,
        "sma10": ma10,
        "sma20": ma20,
        "sma60": ma60,
        "ema12": e12[-1] if e12 else None,
        "ema26": e26[-1] if e26 else None,
        "macd": macd_dif,
        "macd_signal": macd_sig,
        "macd_hist": macd_hist,
        "rsi14": r,
        "atr14": a,
        "atr_pct": (a / last * 100) if a and last else None,
        "vcp_ratio": (a5 / a20) if a5 and a20 and a20 > 0 else None,
        "vol_ratio": (recent_vol / base_vol) if base_vol else None,
        "vol_today_ratio": (today_vol / base_vol) if base_vol else None,
        "high20": high20,
        "low20": low20,
        "high60": high60,
        "low60": low60,
        "dist_sma20": ((last - ma20) / ma20 * 100) if ma20 else None,
        "dist_sma60": ((last - ma60) / ma60 * 100) if ma60 else None,
        "dist_high20": ((last - high20) / high20 * 100) if high20 else None,
        "range_pos20": (
            (last - low20) / (high20 - low20) * 100
            if high20 and low20 and high20 > low20
            else None
        ),
        "range_pos60": (
            (last - low60) / (high60 - low60) * 100
            if high60 and low60 and high60 > low60
            else None
        ),
        "ret5": ret(5),
        "ret20": ret(20),
        "ret60": ret(60),
        "gap_pct": ((open_last - prev_close) / prev_close * 100) if prev_close else 0.0,
        "bull_candle": bool(last >= open_last),
        "close_pos": ((last - series.low[-1]) / day_range) if day_range else 0.5,
        "slope20": slope(close, 20),
        "ma_stack": bool(ma5 and ma20 and ma60 and ma5 > ma20 > ma60),
        "trend_up": bool(ma20 and ma60 and ma20 > ma60 and last > ma60),
        "bars": len(close),
    }


def compute_pair(series: Series) -> tuple[dict, dict | None]:
    """同时返回当前与上一根 K 线的指标快照，用于规则引擎判定金叉/死叉。"""
    curr = compute(series)
    if not curr or len(series) < 31:
        return curr, None
    prev_series = Series(
        close=series.close[:-1],
        high=series.high[:-1],
        low=series.low[:-1],
        volume=series.volume[:-1],
        open=series.open[:-1],
        bar_days=series.bar_days[:-1],
    )
    return curr, compute(prev_series)
