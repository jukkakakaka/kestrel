"""Which bar size suits the current situation?

Only three things decide this, and all of them are already in the database:

* **regime** — what the candidate pool is doing: how many names sit above their
  60-bar average, and how violent the average daily range is;
* **capital** — how much of each trade the fees eat. A small account trading
  weekly cannot pay for the turnover a daily strategy needs;
* **holdings** — how much room is left under the exposure ceiling.

The output is advice, not a lock. ``trade.horizon`` can be pinned by hand, and
when it is pinned this module is not consulted at all. Everything here reads
cached bars — no network, no model — so it can run inside a scan.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kestrel.db.engine import connect
from kestrel.fees.calculator import round_trip
from kestrel.indicators.engine import Series, compute
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import journal as journal_service
from kestrel.services import timeframe

log = logging.getLogger(__name__)

CACHE_KEY = "advisor.horizon"
CACHE_TTL = timedelta(minutes=15)

# Breadth is measured on the pool, not on an index: the pool is what the scanner
# can actually buy, and index membership would drag in names it never sees.
MIN_SAMPLE = 8

FX = {"CNY": Decimal("1"), "HKD": Decimal("0.92"), "USD": Decimal("7.1")}
CURRENCY = {"CN": "CNY", "HK": "HKD", "US": "USD"}


def _reason(code: str, zh: str, en: str, weight: float) -> dict:
    return {"code": code, "zh": zh, "en": en, "weight": weight}


def _breadth(symbols: list[dict], conn) -> dict:
    """Share above the 60-day average, median ATR%, median 20-day return."""
    above = total = 0
    atrs: list[float] = []
    rets: list[float] = []
    latest = ""

    for sym in symbols:
        rows = bars_service.cached_bars(conn, sym["id"], 200)
        if len(rows) < 80:
            continue
        ind = compute(Series.from_bars(rows))
        if not ind or ind.get("sma60") is None:
            continue
        total += 1
        if ind["last"] > ind["sma60"]:
            above += 1
        if ind.get("atr_pct"):
            atrs.append(float(ind["atr_pct"]))
        if ind.get("ret20") is not None:
            rets.append(float(ind["ret20"]))
        latest = max(latest, str(rows[-1]["date"])[:10])

    def median(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        mid = len(ordered) // 2
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    return {
        "sample": total,
        "breadth": (above / total) if total else 0.0,
        "atr_pct": median(atrs),
        "ret20": median(rets),
        "bars_as_of": latest,
    }


def _cost_pct(capital_cny: float, market: str, broker: str | None) -> float:
    """Round-trip fees as a share of a representative ticket.

    A 15%-of-capital position priced at 100 is close enough to the median plan
    this app produces; per-share platform fees need *some* price, and a fake one
    is better than ignoring them.
    """
    currency = CURRENCY.get(market.upper(), "CNY")
    ticket = Decimal(str(max(capital_cny, 1))) * Decimal("0.15") / FX.get(currency, Decimal("1"))
    if ticket <= 0:
        return 0.0
    qty = max(1, int(ticket / Decimal("100")))
    try:
        fees = round_trip(market.upper(), ticket, Decimal(qty), broker)
    except Exception:  # noqa: BLE001
        return 0.0
    return float(fees / ticket * 100)


def _symbols(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.id, s.market, s.code, s.name FROM symbol s
        LEFT JOIN watchlist w ON w.symbol_id = s.id
        WHERE s.is_pool = 1 OR w.id IS NOT NULL
        ORDER BY s.is_pool DESC, s.pool_rank LIMIT 200
        """
    ).fetchall()
    return [dict(r) for r in rows]


def evaluate(force: bool = False) -> dict:
    """Score the three horizons against the current regime and store the result."""
    if not force:
        stored = cached()
        if stored and stored.get("fresh"):
            return stored

    conn = connect()
    try:
        settings_map = config_service.all_values(conn)
        capital = float(settings_map.get("account.capital", 10000) or 10000)
        markets = settings_map.get("scan.markets") or ["CN"]
        market = str(markets[0]) if markets else "CN"
        broker = settings_map.get(f"broker.{market}")
        exposure_pct = float(settings_map.get("risk.max_total_exposure_pct", 60) or 60)
        symbols = _symbols(conn)
        regime = _breadth(symbols, conn)
    finally:
        conn.close()

    scores = {code: 0.0 for code in timeframe.ORDER}
    reasons: list[dict] = []

    def add(codes: dict[str, float], item: dict) -> None:
        for code, weight in codes.items():
            scores[code] += weight
        if any(weight > 0 for weight in codes.values()):
            reasons.append(item)

    breadth = regime["breadth"]
    atr = regime["atr_pct"]
    sample = regime["sample"]

    if sample < MIN_SAMPLE:
        return _store(
            {
                "ok": True,
                "horizon": timeframe.DEFAULT,
                "confidence": 0.0,
                "scores": scores,
                "reasons": [
                    _reason(
                        "no_sample",
                        "候选池历史数据不足，先按日线短线处理。",
                        "Not enough cached history in the pool; staying on the daily default.",
                        0.0,
                    )
                ],
                "metrics": regime | {"capital": capital, "sample": sample},
                "blocked": False,
                "note": "先跑一次扫描把日线缓存起来，建议才有依据。",
            }
        )

    if breadth >= 0.60:
        add(
            {"medium": 1.5, "long": 1.5, "short": 0.5},
            _reason(
                "breadth_strong",
                f"候选池 {breadth * 100:.0f}% 的股票站在 60 日均线上方，趋势面健康，值得给持仓时间。",
                f"{breadth * 100:.0f}% of the pool is above its 60-bar average — trends have room to run.",
                1.5,
            ),
        )
    elif breadth >= 0.35:
        add(
            {"medium": 0.5},
            _reason(
                "breadth_mixed",
                f"只有 {breadth * 100:.0f}% 在 60 日均线上方，普涨不明显，中线优于远线。",
                f"Only {breadth * 100:.0f}% above the 60-bar average — mixed, position over long-term.",
                0.5,
            ),
        )
    else:
        add(
            {"short": 1.0, "long": -2.0},
            _reason(
                "breadth_weak",
                f"只有 {breadth * 100:.0f}% 在 60 日均线上方，弱势市场里长线拿不住，别用月线。",
                f"Only {breadth * 100:.0f}% above the 60-bar average — a weak tape punishes long holds.",
                1.0,
            ),
        )

    if atr >= 4.5:
        add(
            {"short": 2.0, "long": -1.5},
            _reason(
                "vol_high",
                f"候选池日均波幅中位数 {atr:.1f}%，波动太大，止损要近、持仓要短。",
                f"Median daily range is {atr:.1f}% — wide stops and long holds get shaken out.",
                2.0,
            ),
        )
    elif atr and atr <= 2.2:
        add(
            {"long": 1.5, "medium": 0.5},
            _reason(
                "vol_low",
                f"日均波幅中位数只有 {atr:.1f}%，低波环境适合少折腾、拿久一点。",
                f"Median daily range is {atr:.1f}% — calm tape rewards holding instead of churning.",
                1.5,
            ),
        )

    if capital < 30000:
        add(
            {"medium": 1.5, "long": 0.5, "short": -1.0},
            _reason(
                "capital_small",
                f"本金 {capital:,.0f} 偏小，交易成本占比高，减少换手比抓时点更重要。",
                f"Capital of {capital:,.0f} is small — fees dominate, so trade less rather than faster.",
                1.5,
            ),
        )
    elif capital > 300000:
        add(
            {"short": 0.5},
            _reason(
                "capital_large",
                f"本金 {capital:,.0f}，单笔成本压力小，可以承担稍高的换手。",
                f"Capital of {capital:,.0f} absorbs fees well enough for a higher turnover.",
                0.5,
            ),
        )

    cost = _cost_pct(capital, market, broker) or 0.55
    drags = {
        code: cost * (timeframe.get(code).per_year / max(1, timeframe.get(code).max_hold))
        for code in timeframe.ORDER
    }
    if drags["short"] >= 8.0:
        add(
            {"short": -1.5, "medium": 0.5},
            _reason(
                "cost_drag",
                f"按当前券商套餐，日线换手一年成本约 {drags['short']:.1f}%，"
                f"周线只有 {drags['medium']:.1f}%。",
                f"Daily turnover costs about {drags['short']:.1f}% a year in fees; "
                f"weekly costs {drags['medium']:.1f}%.",
                1.5,
            ),
        )

    # Room left under the ceiling is part of "what should I do now", even when it
    # does not change the bar size.
    try:
        open_value = sum(
            (
                journal_service.to_cny(
                    Decimal(str(p["entry"])) * Decimal(str(p["qty"])), p["currency"]
                )
                for p in journal_service.positions()
            ),
            Decimal("0"),
        )
    except Exception:  # noqa: BLE001
        open_value = Decimal("0")
    cap_value = Decimal(str(capital)) * Decimal(str(exposure_pct)) / Decimal("100")
    used_pct = float(open_value / cap_value * 100) if cap_value else 0.0

    blocked = used_pct >= 90
    note = ""
    if blocked:
        note = (
            f"持仓已占用仓位上限的 {used_pct:.0f}%，新一轮扫描里大部分计划会因为敞口上限被拒。"
            "要么先减仓，要么把上限调高——这是账户问题，不是周期问题。"
        )

    order = sorted(
        timeframe.ORDER,
        key=lambda code: (-scores[code], timeframe.ORDER.index(code)),
    )
    best = order[0]
    runner = order[1]
    margin = scores[best] - scores[runner]
    confidence = max(0.0, min(0.9, 0.35 + margin * 0.15))

    primary = next((r for r in reasons if r["weight"] >= 1.5), None) or (
        reasons[0] if reasons else None
    )

    return _store(
        {
            "ok": True,
            "horizon": best,
            "confidence": round(confidence, 2),
            "scores": {code: round(value, 2) for code, value in scores.items()},
            "reasons": reasons,
            "primary_reason": primary["zh"] if primary else "",
            "primary_reason_en": primary["en"] if primary else "",
            "metrics": regime
            | {
                "capital": capital,
                "cost_pct": round(cost, 3),
                "cost_drag": {k: round(v, 2) for k, v in drags.items()},
                "exposure_used_pct": round(used_pct, 1),
                "exposure_cap_pct": exposure_pct,
            },
            "blocked": blocked,
            "note": note,
        }
    )


def _store(report: dict) -> dict:
    report["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    report.setdefault("fresh", True)
    payload = {key: value for key, value in report.items() if key != "fresh"}
    conn = connect()
    try:
        config_service.put(conn, CACHE_KEY, payload)
        conn.commit()
    finally:
        conn.close()
    return report


def cached() -> dict | None:
    conn = connect()
    try:
        stored = config_service.get(conn, CACHE_KEY, None)
    finally:
        conn.close()
    if not stored:
        return None
    try:
        moment = datetime.fromisoformat(str(stored.get("at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return stored | {"fresh": False}
    fresh = datetime.now(UTC) - moment < CACHE_TTL
    return stored | {"fresh": fresh}


def recommend(force: bool = False) -> dict:
    """The horizon to use now: the cache when it is fresh, otherwise recompute."""
    report = evaluate(force=force)
    if not timeframe.is_valid(report.get("horizon")):
        report["horizon"] = timeframe.DEFAULT
    return report


def resolve(requested: str | None) -> tuple[str, dict | None]:
    """Turn a ``trade.horizon`` setting value into a concrete bar size.

    ``auto`` asks this module; anything else is the user's word and wins without
    a single query. Shared by the scanner and the AI brief so both always agree
    on which bars they are talking about.
    """
    code = str(requested or timeframe.AUTO).strip().lower()
    if timeframe.is_valid(code):
        return code, None
    try:
        report = recommend()
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor failed, staying on %s: %s", timeframe.DEFAULT, exc)
        return timeframe.DEFAULT, None
    return report.get("horizon") or timeframe.DEFAULT, report
