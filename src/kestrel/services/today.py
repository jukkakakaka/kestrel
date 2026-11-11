"""今天该做什么。

新手打开一个股票工具，最常问的不是「哪只票好」，而是「我现在该干嘛」。
这个模块把数据库里已经存在的事实汇总成一张待办清单，每条都带动作和链接：

* 有持仓跌破止损 → 立刻处理，这是唯一真正紧急的事；
* 有建议快过期 → 想买就趁现在，过期后价格条件已经变了；
* 有建议还在有效期内 → 去看看；
* 什么都没发生 → 明确告诉你「今天不用动」，这也是一个结论。

所有判断都基于本地数据，不需要网络。行情缺失时宁可说「没有现价」，
也不猜一个数字出来。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kestrel.db.engine import connect
from kestrel.services import positions as position_service

log = logging.getLogger(__name__)

URGENT = "urgent"
TODO = "todo"
INFO = "info"


def _hours_left(iso_text: str | None) -> float | None:
    if not iso_text:
        return None
    try:
        moment = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment - datetime.now(UTC)).total_seconds() / 3600


def briefing() -> dict:
    """组装首页待办。返回 ``{items, counts, headline, quiet}``。"""
    items: list[dict] = []

    # 1. 持仓风险永远是第一位
    try:
        book = position_service.open_positions()
    except Exception as exc:  # noqa: BLE001
        log.warning("positions unavailable: %s", exc)
        book = []
    summary = position_service.summary(book)

    for p in summary["risky"]:
        if p["state"] == "stopped":
            items.append(
                {
                    "kind": URGENT,
                    "icon": "!",
                    "title": f"{p['name']} 已跌破止损 {p['stop']:.2f}",
                    "body": (
                        f"现价 {p['price']:.2f}，持仓 {p['qty']} 股。按计划现在就该卖出。"
                        if p["price"]
                        else "价格已跌破止损位，按计划应当卖出。"
                    ),
                    "action": {"label": "记录卖出", "href": f"/stock/{p['market']}/{p['code']}"},
                    "tag": "止损",
                }
            )
        elif p["state"] == "near_stop":
            items.append(
                {
                    "kind": URGENT,
                    "icon": "!",
                    "title": f"{p['name']} 接近止损 {p['stop']:.2f}",
                    "body": (
                        f"现价 {p['price']:.2f}，距止损只剩 {p['to_stop_pct']:.2f}%。"
                        "提前想好到价就走，不要临场改主意。"
                        if p["price"]
                        else "已经贴近止损位。"
                    ),
                    "action": {"label": "看一下", "href": "/journal"},
                    "tag": "风险",
                }
            )

    for p in book:
        if p["state"] in ("target", "near_target") and p["state"] != "near_stop":
            items.append(
                {
                    "kind": TODO,
                    "icon": "✓",
                    "title": f"{p['name']} 到达目标价 {p['target']:.2f}"
                    if p["state"] == "target"
                    else f"{p['name']} 接近目标价 {p['target']:.2f}",
                    "body": "计划里的目标已到。常见做法是先卖一半，剩下用移动止损跟。",
                    "action": {"label": "记录卖出", "href": "/journal"},
                    "tag": "止盈",
                }
            )

    # 2. 有效期内、还没处理的建议
    conn = connect()
    try:
        recs = conn.execute(
            "SELECT id, market, code, name, currency, qty, entry_low, entry_high, stop,"
            " target1, valid_until, horizon, source"
            " FROM recommendation WHERE status='active'"
            " ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()

    soon = []
    later = []
    for rec in recs:
        left = _hours_left(rec["valid_until"])
        row = dict(rec)
        row["hours_left"] = left
        if left is None:
            later.append(row)
        elif left <= 12:
            soon.append(row)
        else:
            later.append(row)

    for rec in soon:
        entry_low = rec["entry_low"] / 1_000_000
        items.append(
            {
                "kind": TODO,
                "icon": "→",
                "title": f"{rec['name']} 的建议快过期了",
                "body": (
                    f"入场区间 {entry_low:.3f} 附近，还剩约 "
                    f"{max(0, round(rec['hours_left'] or 0)):.0f} 小时。过期后价格条件可能已经变了。"
                ),
                "action": {"label": "现在处理", "href": f"/advice#rec-{rec['id']}"},
                "tag": "建议",
            }
        )

    if not soon and later:
        items.append(
            {
                "kind": INFO,
                "icon": "·",
                "title": f"有 {len(later)} 条建议在有效期内",
                "body": "不着急，可以先看清楚再决定。今天不交易也是正确答案。",
                "action": {"label": "看建议", "href": "/advice"},
                "tag": "建议",
            }
        )

    # 3. 账户状态
    if summary["unpriced"]:
        items.append(
            {
                "kind": INFO,
                "icon": "·",
                "title": f"{summary['unpriced']} 笔持仓没有现价",
                "body": "去行情页刷新一次，浮动盈亏和止损距离才算得准。",
                "action": {"label": "刷行情", "href": "/market"},
                "tag": "数据",
            }
        )

    if not book and not recs:
        items.append(
            {
                "kind": TODO,
                "icon": "1",
                "title": "先跑一次扫描，看看有没有值得做的",
                "body": "扫描会按你的周期和本金，从候选池里挑出满足条件的标的，并算好止损、目标和股数。",
                "action": {"label": "去扫描", "href": "/advice"},
                "tag": "开始",
            }
        )

    urgent = sum(1 for item in items if item["kind"] == URGENT)
    if urgent:
        headline = f"有 {urgent} 件事需要现在处理"
    elif any(item["kind"] == TODO for item in items):
        headline = "有几件事值得看一眼，但都不急"
    else:
        headline = "今天不用动手"

    return {
        "todos": items,
        "urgent": urgent,
        "headline": headline,
        "quiet": not items,
        "book": {
            "count": summary["count"],
            "pnl_cny": float(summary["pnl_cny"]),
            "pnl_pct": summary["pnl_pct"],
            "cost_cny": float(summary["cost_cny"]),
        },
        "positions": book[:6],
        "active_advice": len(recs),
    }
