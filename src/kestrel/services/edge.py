"""The ``/api/edge`` report: a view over the one measurement that exists.

This module used to run a second, independent backtest with its own thresholds —
thirty trades, no folds, no benchmark — and its own opinion about what "usable"
meant. Its docstring even claimed the scanner refused advice based on it, while
nothing called it at all. Two answers to one question is worse than none, and a
gate that is not wired up is worse than either.

It now delegates to :mod:`kestrel.services.rules`, which is what the rules page
and the scanner have been using all along.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kestrel.db.engine import connect
from kestrel.services import config as config_service
from kestrel.services import rules as rule_service
from kestrel.services import timeframe

log = logging.getLogger(__name__)

CACHE_KEY = "edge.report"


def evaluate(max_symbols: int = 120, horizon: str = "short") -> dict:
    """Measure every rule on one bar size, store the summary, return it."""
    cfg = timeframe.get(horizon)
    results = rule_service.validate_all(horizon=cfg.code, max_symbols=max_symbols)

    strategies: dict[str, dict] = {}
    for item in results:
        code = item.get("code") or "?"
        if not item.get("ok"):
            strategies[code] = {"verdict": "failed", "error": item.get("error")}
            continue
        strategies[code] = {
            "verdict": item["verdict"],
            "expectancy": item["avg_exp"],
            "edge": item["avg_edge"],
            "trades": item.get("trades", 0),
            "folds": len(item.get("folds") or []),
            # False means the measurement was too thin to be stored; the API
            # says so rather than pretending the numbers were saved.
            "persisted": bool(item.get("persisted", True)),
        }

    measured = sum(1 for entry in strategies.values() if entry.get("verdict") != "failed")
    saved = sum(1 for entry in strategies.values() if entry.get("persisted"))
    skipped = measured - saved
    if skipped and saved:
        note = f"{skipped} 条规则样本不足，未覆盖原判定。"
    elif skipped:
        note = "全部规则样本不足，未覆盖原判定；换更大的候选池再试。"
    else:
        note = ""
    summary = {
        "ok": True,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "horizon": cfg.code,
        "label": cfg.zh,
        "persisted": saved,
        "skipped": skipped,
        "note": note,
        "strategies": strategies,
    }
    conn = connect()
    try:
        config_service.put(conn, CACHE_KEY, summary)
        conn.commit()
    finally:
        conn.close()
    return summary


def cached() -> dict | None:
    conn = connect()
    try:
        return config_service.get(conn, CACHE_KEY, None)
    finally:
        conn.close()
