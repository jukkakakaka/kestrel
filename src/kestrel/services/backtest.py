"""Minimal event-driven backtest over daily bars.

Deliberately shares the same strategies and plan sizing as the live scan — if you
change a rule, the backtest changes with it. `LookaheadError` guard: indicators are
computed on the prefix ending at the signal bar, never on the full series.
"""

from __future__ import annotations

from decimal import Decimal

from kestrel.indicators.engine import Series, compute
from kestrel.strategies.base import registry


class LookaheadError(RuntimeError):
    pass


def run(bars: list[dict], strategy_code: str, max_hold: int = 15, min_bars: int = 60) -> dict:
    strategy = registry().get(strategy_code)
    if strategy is None:
        raise ValueError(f"unknown strategy {strategy_code}")
    if len(bars) <= min_bars:
        return {"trades": [], "stats": _stats([]), "equity": []}

    trades: list[dict] = []
    equity: list[float] = []
    r_total = 0.0
    position: dict | None = None

    for i in range(min_bars, len(bars)):
        window = bars[: i + 1]
        if position is None:
            ind = compute(Series.from_bars(window))
            if not ind:
                continue
            signal = strategy.evaluate(ind)
            if signal is None:
                continue
            entry = float(bars[i]["close"])
            stop = float(signal.stop_hint) if signal.stop_hint and signal.stop_hint < entry else entry * 0.97
            if stop >= entry:
                continue
            risk = entry - stop
            position = {
                "index": i,
                "entry": entry,
                "stop": stop,
                "target": entry + risk * 2,
                "risk": risk,
            }
            continue

        bar = bars[i]
        held = i - position["index"]
        if bar["low"] <= position["stop"]:
            r = -1.0
            trades.append({**position, "exit": position["stop"], "bars": held, "r": r})
            r_total += r
            position = None
        elif bar["high"] >= position["target"]:
            r = 2.0
            trades.append({**position, "exit": position["target"], "bars": held, "r": r})
            r_total += r
            position = None
        elif held >= max_hold:
            r = (float(bar["close"]) - position["entry"]) / position["risk"]
            trades.append({**position, "exit": float(bar["close"]), "bars": held, "r": r})
            r_total += r
            position = None
        equity.append(round(r_total, 4))

    if position is not None:
        last = float(bars[-1]["close"])
        r = (last - position["entry"]) / position["risk"]
        trades.append({**position, "exit": last, "bars": len(bars) - 1 - position["index"], "r": r})
        equity.append(round(r_total + r, 4))

    return {"trades": trades, "stats": _stats(trades), "equity": equity}


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"count": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0, "max_dd": 0.0}
    wins = [t for t in trades if t["r"] > 0]
    total_r = sum(t["r"] for t in trades)
    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for t in trades:
        running += t["r"]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    return {
        "count": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_r": total_r / len(trades),
        "total_r": total_r,
        "max_dd": max_dd,
    }


def save(market: str, code: str, strategy: str, result: dict, start: str, end: str) -> int:
    import json

    from kestrel.db.engine import connect

    stats = result["stats"]
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO backtest_run (market, code, strategy, start_date, end_date,
                                      trades, win_rate, avg_r, max_dd, equity)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market, code, strategy, start, end, stats["count"], stats["win_rate"],
                stats["avg_r"], stats["max_dd"], json.dumps(result["equity"][-120:]),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent(limit: int = 20) -> list[dict]:
    from kestrel.db.engine import connect

    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM backtest_run ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
    finally:
        conn.close()


def to_decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 4)))
