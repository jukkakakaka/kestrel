"""Does a strategy actually make money? Answer before recommending anything.

A rule that "looks sensible" is not an edge. Until a strategy has been run across
many symbols and many months, nobody knows whether following it earns or loses.
This module measures that, and the scanner refuses to publish advice from a
strategy that has not earned it.

The measure used is expectancy in R: the average result of one trade expressed in
multiples of what was risked. +0.15R means that risking 100 a trade returns 15 on
average. It is the only number that survives different position sizes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kestrel.db.engine import connect
from kestrel.services import backtest
from kestrel.services import bars as bars_service
from kestrel.services import rules as rule_service

log = logging.getLogger(__name__)

# A strategy needs at least this many closed trades before its numbers mean
# anything; below it we say "insufficient evidence" rather than "good".
MIN_TRADES = 30
# Expectancy floor. Below zero the rule loses money after the spread, and no
# amount of signal quality fixes that.
MIN_EXPECTANCY = 0.0


def _aggregate(trades: list[dict]) -> dict:
    if not trades:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "expectancy": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_r": 0.0,
            "max_loss_streak": 0,
        }

    wins = [t["r"] for t in trades if t["r"] > 0]
    losses = [t["r"] for t in trades if t["r"] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    running = peak = dd = 0.0
    streak = worst_streak = 0
    for t in trades:
        running += t["r"]
        peak = max(peak, running)
        dd = min(dd, running - peak)
        if t["r"] <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "expectancy": sum(t["r"] for t in trades) / len(trades),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else 99.0,
        "max_drawdown_r": dd,
        "max_loss_streak": worst_streak,
    }


def evaluate(max_symbols: int = 120, history: int = 500) -> dict:
    """Run every strategy across the stored universe and aggregate the results."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, market, code, name FROM symbol
            WHERE (is_pool=1 OR id IN (SELECT symbol_id FROM watchlist))
            ORDER BY is_pool DESC, pool_rank
            LIMIT ?
            """,
            (max_symbols,),
        ).fetchall()

        series: list[tuple[dict, list[dict]]] = []
        for row in rows:
            bars = bars_service.cached_bars(conn, row["id"], history)
            if len(bars) >= 80:
                series.append((dict(row), bars))
    finally:
        conn.close()

    if not series:
        return {
            "ok": False,
            "error": "没有可用的历史数据，先跑一次扫描把日线缓存起来。",
            "strategies": {},
        }

    rules = rule_service.registry()
    report: dict[str, dict] = {}
    for code in rules:
        trades: list[dict] = []
        per_symbol: list[dict] = []
        for sym, bars in series:
            try:
                result = backtest.run(bars, code, registry_override=rules)
            except Exception as exc:  # noqa: BLE001
                log.warning("backtest failed %s %s: %s", code, sym["code"], exc)
                continue
            for trade in result["trades"]:
                trades.append(trade)
            if result["stats"]["count"]:
                per_symbol.append(
                    {
                        "code": sym["code"],
                        "market": sym["market"],
                        "trades": result["stats"]["count"],
                        "total_r": round(result["stats"]["total_r"], 2),
                    }
                )

        agg = _aggregate(trades)
        agg["verdict"] = _verdict(agg)
        agg["usable"] = _usable(agg)
        agg["best"] = sorted(per_symbol, key=lambda x: -x["total_r"])[:3]
        agg["worst"] = sorted(per_symbol, key=lambda x: x["total_r"])[:3]
        report[code] = agg

    summary = {
        "ok": True,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "symbols": len(series),
        "bars": sum(len(b) for _, b in series),
        "strategies": report,
    }
    conn = connect()
    try:
        from kestrel.services import config as config_service

        config_service.put(conn, "edge.report", summary)
        conn.commit()
    finally:
        conn.close()
    return summary


def _verdict(agg: dict) -> str:
    if agg["trades"] < MIN_TRADES:
        return "insufficient"
    if agg["expectancy"] < MIN_EXPECTANCY:
        return "losing"
    if agg["expectancy"] < 0.1:
        return "marginal"
    return "positive"


def _usable(agg: dict) -> bool:
    return agg["trades"] >= MIN_TRADES and agg["expectancy"] > MIN_EXPECTANCY


def cached() -> dict | None:
    conn = connect()
    try:
        from kestrel.services import config as config_service

        return config_service.get(conn, "edge.report", None)
    finally:
        conn.close()


def enabled_strategies() -> dict[str, bool]:
    """Which strategies are currently allowed to publish advice."""
    report = cached()
    if not report or not report.get("strategies"):
        # No evidence yet: allow everything rather than silently going dark,
        # but the scan labels the result as unproven.
        return {code: True for code in rule_service.registry()}
    return {code: bool(stats.get("usable")) for code, stats in report["strategies"].items()}


def describe(agg: dict) -> str:
    """One sentence a beginner can use."""
    if agg["trades"] == 0:
        return "在现有历史数据里一次也没有触发，无法评价。"
    if agg["trades"] < MIN_TRADES:
        return f"只触发过 {agg['trades']} 次，样本太小，还不能下结论。"
    exp = agg["expectancy"]
    if exp <= 0:
        return (
            f"过去 {agg['trades']} 次交易，平均每笔 {exp:+.2f}R，"
            f"也就是长期亏损。这套规则目前不该用。"
        )
    return (
        f"过去 {agg['trades']} 次交易，胜率 {agg['win_rate'] * 100:.0f}%，"
        f"平均每笔 {exp:+.2f}R，最大连亏 {agg['max_loss_streak']} 次。"
    )
