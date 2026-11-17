"""交易周期定义与日线聚合（超短 / 短线 / 中线 / 长线）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Horizon:
    code: str
    zh: str
    en: str
    bar: str                      # D | W | M
    lookback: int                 # 需要缓存的日线条数
    min_bars: int                 # 聚合后至少需要的 K 线根数
    warmup: int                   # 回测预热根数
    min_span: int                 # 滚动测试最短单折长度
    max_hold: int                 # 时间止损（以当前周期 K 线数计）
    ttl_days: int                 # 建议有效期（日历天，0 表示下一个收盘）
    stop_band: tuple[float, float] = (0.015, 0.15)
    beta: bool = False

    @property
    def per_year(self) -> int:
        return {"D": 245, "W": 50, "M": 12}.get(self.bar, 245)

    @property
    def max_hold_days(self) -> int:
        per_bar = {"D": 7 / 5, "W": 7, "M": 30}.get(self.bar, 1)
        return int(self.max_hold * per_bar) + (2 if self.bar == "D" else 0)


HORIZONS: dict[str, Horizon] = {
    "fast": Horizon(
        code="fast", zh="超短 · 快波段", en="Fast · swing", bar="D",
        lookback=240, min_bars=40, warmup=80, min_span=30, max_hold=5, ttl_days=0,
        stop_band=(0.012, 0.08),
    ),
    "short": Horizon(
        code="short", zh="短线 · 日线", en="Swing · daily", bar="D",
        lookback=300, min_bars=40, warmup=80, min_span=40, max_hold=15, ttl_days=0,
    ),
    "medium": Horizon(
        code="medium", zh="中线 · 周线", en="Position · weekly", bar="W",
        lookback=1200, min_bars=65, warmup=61, min_span=30, max_hold=8, ttl_days=10,
        stop_band=(0.03, 0.18),
    ),
    "long": Horizon(
        code="long", zh="长线 · 月线", en="Long · monthly", bar="M",
        lookback=4000, min_bars=65, warmup=61, min_span=24, max_hold=6, ttl_days=45,
        stop_band=(0.05, 0.30), beta=True,
    ),
}

DEFAULT = "short"
ORDER = ("fast", "short", "medium", "long")
AUTO = "auto"


def get(code: str | None) -> Horizon:
    """解析周期代码，无效时回退至默认短线日线。"""
    return HORIZONS.get(str(code or "").strip().lower(), HORIZONS[DEFAULT])


def is_valid(code: str | None) -> bool:
    return str(code or "").strip().lower() in HORIZONS


def label(code: str | None, locale: str = "zh") -> str:
    horizon = get(code)
    return horizon.en if str(locale).lower().startswith("en") else horizon.zh


def options(locale: str = "zh") -> list[dict]:
    out = []
    for code in ORDER:
        horizon = HORIZONS[code]
        out.append(
            {
                "code": code,
                "label": label(code, locale),
                "beta": horizon.beta,
                "bar": horizon.bar,
                "max_hold": horizon.max_hold,
                "ttl_days": horizon.ttl_days,
            }
        )
    return out


def _bucket(day: date, bar: str) -> tuple[int, ...]:
    if bar == "W":
        iso = day.isocalendar()
        return (iso[0], iso[1])
    if bar == "M":
        return (day.year, day.month)
    return (day.toordinal(),)


def aggregate(bars: list[dict], bar: str) -> list[dict]:
    """将日线折叠为周线或月线 OHLCV，并记录每根聚合线包含的交易日数 ``bar_days``。"""
    if bar not in ("W", "M") or len(bars) < 2:
        return bars

    out: list[dict] = []
    current: dict | None = None
    bucket: tuple[int, ...] | None = None

    for raw in bars:
        try:
            day = date.fromisoformat(str(raw["date"])[:10])
        except (KeyError, ValueError):
            continue
        key = _bucket(day, bar)
        high = float(raw["high"])
        low = float(raw["low"])
        close = float(raw["close"])
        volume = float(raw.get("volume") or 0)
        turnover = float(raw.get("turnover") or 0)

        if key != bucket or current is None:
            if current is not None:
                out.append(current)
            bucket = key
            current = {
                "date": str(raw["date"]),
                "open": float(raw["open"]),
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "turnover": turnover,
                "bar_days": 1,
            }
            continue

        current["date"] = str(raw["date"])
        current["high"] = max(current["high"], high)
        current["low"] = min(current["low"], low)
        current["close"] = close
        current["volume"] += volume
        current["turnover"] += turnover
        current["bar_days"] += 1

    if current is not None:
        out.append(current)
    return out


def prepare(bars: list[dict], code: str | None) -> list[dict]:
    """按交易周期聚合日线。"""
    return aggregate(bars, get(code).bar)
