"""Trading horizons and bar aggregation.

The tool shipped as a daily-only swing tool: one bar table, one lookback, one
holding period, one plan lifetime. The signal code itself never cared about the
bar size, so the same rules make sense on weekly and monthly bars — but only if
everything that *is* frequency-dependent moves together: how much history we
fetch, how long a position may be held in the backtest, how wide a stop may be,
and how long a plan stays valid.

``short`` reproduces the original daily behaviour exactly, numbers included. That
is deliberate: existing users' backtests must not silently change meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Horizon:
    code: str
    zh: str
    en: str
    bar: str                      # D | W | M
    lookback: int                 # daily bars to keep cached — drives the fetch size
    min_bars: int                 # bars required after aggregation
    warmup: int                   # bars burned before any fold may start
    min_span: int                 # shortest usable fold, in bars
    max_hold: int                 # time-stop, counted in this horizon's bars
    ttl_days: int                 # plan lifetime in calendar days, 0 = next session close
    stop_band: tuple[float, float] = (0.015, 0.15)
    beta: bool = False

    @property
    def per_year(self) -> int:
        """Bars per year — used to turn a holding period into a turnover estimate."""
        return {"D": 245, "W": 50, "M": 12}.get(self.bar, 245)


HORIZONS: dict[str, Horizon] = {
    "short": Horizon(
        code="short", zh="短线 · 日线", en="Swing · daily", bar="D",
        lookback=300, min_bars=40, warmup=80, min_span=40, max_hold=15, ttl_days=0,
    ),
    # Warmup is >= 61 bars everywhere on purpose: the indicator set is built
    # around a 60-bar average, so fewer than that and every built-in rule sees
    # None and silently never fires. Monthly only works because the provider
    # hands back ~25 years of dailies, which is ~190 bars.
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
ORDER = ("short", "medium", "long")
AUTO = "auto"


def get(code: str | None) -> Horizon:
    """Resolve a horizon code, falling back to the daily default."""
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
    """Fold daily bars into weekly/monthly OHLCV.

    The final period is kept even when it is still forming — that is the state a
    live scan has to decide on. A weekly trader looking at Wednesday sees a
    partial week, and so does the scanner; hiding it would make the tool act on
    a week that ended five days ago.
    """
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
            }
            continue

        current["date"] = str(raw["date"])
        current["high"] = max(current["high"], high)
        current["low"] = min(current["low"], low)
        current["close"] = close
        current["volume"] += volume
        current["turnover"] += turnover

    if current is not None:
        out.append(current)
    return out


def prepare(bars: list[dict], code: str | None) -> list[dict]:
    """Aggregate daily bars to the bar size a horizon trades on."""
    return aggregate(bars, get(code).bar)
