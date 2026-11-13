"""持仓盯盘：算清楚「现在赚多少、离止损还有多远、计划是不是过期了」。

扫描页回答「买什么」，这个模块回答「手里这些怎么办」——对新手来说后者更急，
因为卖出决策每天都在，而买入机会可以等。

所有价格都来自 ``quote_latest`` 缓存，不会为了算盈亏去请求网络；
行情缺失时显示 ``—`` 而不是 0，那才是诚实的。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from kestrel.core.money import from_micro
from kestrel.db.engine import connect
from kestrel.services import journal

log = logging.getLogger(__name__)

NEAR_STOP_PCT = 2.0      # 距止损不到 2% 就算「危险区」
NEAR_TARGET_PCT = 2.0


def _quote_map(conn, keys: list[tuple[str, str]]) -> dict[str, dict]:
    if not keys:
        return {}
    out: dict[str, dict] = {}
    for market, code in keys:
        row = conn.execute(
            """
            SELECT q.price, q.prev_close, q.change_pct, q.source, q.fetched_at
            FROM quote_latest q JOIN symbol s ON s.id = q.symbol_id
            WHERE s.market=? AND s.code=?
            """,
            (market, code),
        ).fetchone()
        if row and row["price"]:
            out[f"{market}:{code}"] = dict(row)
    return out


def _plan_from_snapshot(trade: dict) -> dict | None:
    import json

    raw = trade.get("plan_snapshot")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _parse_moment(text: str | None) -> datetime | None:
    """解析时间戳，兼容 SQLite 的 ``YYYY-MM-DD HH:MM:SS``（无时区）。

    旧行由 ``datetime('now')`` 写入，是 UTC 但不带时区标记；直接和
    ``datetime.now(UTC)`` 相减会抛 TypeError。统一按 UTC 处理。
    """
    if not text:
        return None
    cleaned = str(text).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def assess(trade: dict, quote: dict | None) -> dict:
    """一笔持仓的现状。纯函数，方便单测和复用。"""
    entry = from_micro(trade["entry_price"])
    qty = Decimal(str(trade["qty"]))
    cost = entry * qty
    fees = from_micro(trade.get("fees") or 0)
    stop = from_micro(trade["stop_price"]) if trade.get("stop_price") else None
    target = from_micro(trade["target_price"]) if trade.get("target_price") else None

    price = from_micro(quote["price"]) if quote and quote.get("price") else None
    change_pct = float(quote["change_pct"]) if quote and quote.get("change_pct") is not None else None

    plan = _plan_from_snapshot(trade) or {}
    if stop is None and plan.get("stop"):
        stop = from_micro(plan["stop"])
    if target is None and plan.get("target"):
        target = from_micro(plan["target"])

    pnl = pnl_pct = None
    if price is not None:
        pnl = (price - entry) * qty - fees
        pnl_pct = float((price - entry) / entry * 100) if entry else None

    to_stop_pct = None
    if price is not None and stop:
        to_stop_pct = float((price - stop) / price * 100)

    to_target_pct = None
    if price is not None and target:
        to_target_pct = float((target - price) / price * 100)

    r_now = None
    # R 倍数永远按**最初**的风险算。止损上移之后如果还用当前止损做分母，
    # +2R 会被算成 +4R，看起来越来越好，实际判断依据已经没了。
    initial_raw = trade.get("initial_stop") or trade.get("stop_price") or plan.get("stop")
    initial_stop = from_micro(initial_raw) if initial_raw else None
    if price is not None and initial_stop and entry > initial_stop:
        r_now = float((price - entry) / (entry - initial_stop))

    opened = str(trade.get("filled_at") or trade.get("opened_at") or "")
    held_days = None
    start = _parse_moment(opened)
    if start is not None:
        held_days = (datetime.now(UTC) - start).days

    overdue = None
    if plan.get("valid_until"):
        moment = _parse_moment(plan["valid_until"])
        if moment is not None:
            overdue = datetime.now(UTC) > moment

    state = "ok"
    if price is None:
        state = "no_quote"
    elif stop and price <= stop:
        state = "stopped"
    elif stop and to_stop_pct is not None and to_stop_pct <= NEAR_STOP_PCT:
        state = "near_stop"
    elif target and price >= target:
        state = "target"
    elif target and to_target_pct is not None and to_target_pct <= NEAR_TARGET_PCT:
        state = "near_target"

    return {
        "trade_id": trade["id"],
        "market": trade["market"],
        "code": trade["code"],
        "name": trade["name"],
        "currency": trade["currency"],
        "qty": qty,
        "entry": entry,
        "price": price,
        "cost": cost,
        "fees": fees,
        "stop": stop,
        "target": target,
        "change_pct": change_pct,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "to_stop_pct": to_stop_pct,
        "to_target_pct": to_target_pct,
        "r_now": r_now,
        "held_days": held_days,
        "overdue": overdue,
        "state": state,
        "rec_id": trade.get("rec_id"),
        "plan": plan or None,
        "source": trade.get("source") or "manual",
        "opened_at": trade.get("filled_at") or trade.get("opened_at"),
    }


STATE_LABELS = {
    "ok": ("持有中", "Holding"),
    "no_quote": ("没有现价", "No quote"),
    "near_stop": ("接近止损", "Near stop"),
    "stopped": ("已跌破止损", "Below stop"),
    "near_target": ("接近目标", "Near target"),
    "target": ("已到目标", "At target"),
}


def open_positions(include_quotes: bool = True, with_exits: bool = True) -> list[dict]:
    """所有未平仓持仓，附带现价、盈亏，以及一条「现在该不该动」的建议。"""
    from kestrel.services import exits as exit_service
    from kestrel.services import timeframe

    rows = journal.list_trades(status="open", limit=500)
    for row in rows:
        # 兼容旧行：filled_at 为空时回退 opened_at
        row.setdefault("filled_at", None)
    conn = connect()
    try:
        quotes = _quote_map(conn, [(r["market"], r["code"]) for r in rows]) if include_quotes else {}
    finally:
        conn.close()

    out = [assess(row, quotes.get(f"{row['market']}:{row['code']}")) for row in rows]
    if with_exits:
        by_id = {row["id"]: row for row in rows}
        for position in out:
            row = by_id.get(position["trade_id"])
            if row is None:
                continue
            horizon = (position.get("plan") or {}).get("horizon") or timeframe.DEFAULT
            try:
                plan = exit_service.suggest(
                    row, position, max_hold_days=timeframe.get(horizon).max_hold_days
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("exit advice failed for trade %s: %s", position["trade_id"], exc)
                plan = None
            position["exit"] = plan.as_dict() if plan is not None else None
    return out


def summary(positions: list[dict]) -> dict:
    """组合层面的数字：总成本、总盈亏、最危险的几笔。"""
    cost_cny = Decimal("0")
    pnl_cny = Decimal("0")
    priced = 0
    risky: list[dict] = []
    for p in positions:
        fx = journal.FX.get(p["currency"], Decimal("1"))
        cost_cny += p["cost"] * fx
        if p["pnl"] is not None:
            priced += 1
            pnl_cny += p["pnl"] * fx
        if p["state"] in ("stopped", "near_stop"):
            risky.append(p)

    # 按「距止损距离」排序，越近越急；没有止损的排在最后。
    risky.sort(key=lambda p: (p["to_stop_pct"] is None, p["to_stop_pct"] or 0))
    return {
        "count": len(positions),
        "priced": priced,
        "cost_cny": cost_cny,
        "pnl_cny": pnl_cny,
        "pnl_pct": float(pnl_cny / cost_cny * 100) if cost_cny else None,
        "risky": risky[:5],
        "unpriced": len(positions) - priced,
    }


def sweep_alerts() -> list[dict]:
    """找出需要提醒的持仓，并保证同一件事只提醒一次。

    返回待发送的提醒列表；真正的发送由调度器通过已有通知渠道完成，
    这个函数只做判定与去重，方便测试。
    """
    from kestrel.core.money import to_micro

    positions = open_positions()
    pending: list[dict] = []
    conn = connect()
    try:
        for p in positions:
            kind = {
                "stopped": "stop_hit",
                "near_stop": "near_stop",
                "target": "target_hit",
                "near_target": "near_target",
            }.get(p["state"])
            if kind is None:
                continue
            exists = conn.execute(
                "SELECT 1 FROM position_alert WHERE trade_id=? AND kind=?",
                (p["trade_id"], kind),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO position_alert (trade_id, kind, price)"
                " VALUES (?,?,?)",
                (p["trade_id"], kind, to_micro(p["price"]) if p["price"] else None),
            )
            pending.append({**p, "kind": kind})
        conn.commit()
    finally:
        conn.close()
    return pending


ALERT_TEXT = {
    "stopped": {
        "title": "已跌破止损",
        "body": "按计划应当卖出。止损不是认输，是让你还有下一次机会。",
    },
    "near_stop": {
        "title": "接近止损",
        "body": "价格已经贴近止损位。提前想好：到价就走，不要临场改主意。",
    },
    "target": {
        "title": "已到目标价",
        "body": "计划里写的目标已经到达。常见做法是先卖掉一半，剩下用移动止损跟。",
    },
    "near_target": {
        "title": "接近目标价",
        "body": "离目标不远了。可以准备分批止盈。",
    },
}
