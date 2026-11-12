"""今天该做什么。

新手打开一个股票工具，最常问的不是「哪只票好」，而是「我现在该干嘛」。
这个模块把数据库里已经存在的事实汇总成一张待办清单，每条都带动作和链接：

* 有持仓跌破止损 → 立刻处理，这是唯一真正紧急的事；
* 有建议快过期 → 想买就趁现在，过期后价格条件已经变了；
* 有建议还在有效期内 → 去看看；
* 什么都没发生 → 明确告诉你「今天不用动」，这也是一个结论。

所有判断都基于本地数据，不需要网络。行情缺失时宁可说「没有现价」，
也不猜一个数字出来。

文案全部按 locale 生成：英文界面显示中文结论比不显示更糟。
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


def briefing(locale: str = "zh-CN") -> dict:
    """组装首页待办。返回 ``{todos, urgent, headline, ...}``。"""
    en = str(locale).lower().startswith("en")
    t = _text(en)
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
                    "title": t("stopped_title", name=p["name"], stop=p["stop"] or 0),
                    "body": (
                        t("stopped_body_price", price=p["price"], qty=p["qty"])
                        if p["price"]
                        else t("stopped_body")
                    ),
                    "action": {"label": t("sell"), "href": f"/stock/{p['market']}/{p['code']}"},
                    "tag": t("tag_stop"),
                }
            )
        elif p["state"] == "near_stop":
            items.append(
                {
                    "kind": URGENT,
                    "icon": "!",
                    "title": t("near_stop_title", name=p["name"], stop=p["stop"] or 0),
                    "body": (
                        t("near_stop_body_price", price=p["price"], pct=p["to_stop_pct"] or 0)
                        if p["price"]
                        else t("near_stop_body")
                    ),
                    "action": {"label": t("look"), "href": "/journal"},
                    "tag": t("tag_risk"),
                }
            )

    for p in book:
        if p["state"] in ("target", "near_target"):
            items.append(
                {
                    "kind": TODO,
                    "icon": "✓",
                    "title": t(
                        "target_title" if p["state"] == "target" else "near_target_title",
                        name=p["name"],
                        target=p["target"] or 0,
                    ),
                    "body": t("target_body"),
                    "action": {"label": t("sell"), "href": "/journal"},
                    "tag": t("tag_profit"),
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
        if left is not None and left <= 12:
            soon.append(row)
        else:
            later.append(row)

    for rec in soon:
        items.append(
            {
                "kind": TODO,
                "icon": "→",
                "title": t("expiring_title", name=rec["name"]),
                "body": t(
                    "expiring_body",
                    entry=rec["entry_low"] / 1_000_000,
                    hours=max(0, round(rec["hours_left"] or 0)),
                ),
                "action": {"label": t("handle"), "href": f"/advice#rec-{rec['id']}"},
                "tag": t("tag_advice"),
            }
        )

    if not soon and later:
        items.append(
            {
                "kind": INFO,
                "icon": "·",
                "title": t("pending_title", n=len(later)),
                "body": t("pending_body"),
                "action": {"label": t("see_advice"), "href": "/advice"},
                "tag": t("tag_advice"),
            }
        )

    # 3. 账户状态
    if summary["unpriced"]:
        items.append(
            {
                "kind": INFO,
                "icon": "·",
                "title": t("unpriced_title", n=summary["unpriced"]),
                "body": t("unpriced_body"),
                "action": {"label": t("refresh"), "href": "/market"},
                "tag": t("tag_data"),
            }
        )

    if not book and not recs:
        items.append(
            {
                "kind": TODO,
                "icon": "1",
                "title": t("start_title"),
                "body": t("start_body"),
                "action": {"label": t("start_action"), "href": "/advice"},
                "tag": t("tag_start"),
            }
        )

    urgent = sum(1 for item in items if item["kind"] == URGENT)
    if urgent:
        headline = t("headline_urgent", n=urgent)
    elif any(item["kind"] == TODO for item in items):
        headline = t("headline_soft")
    else:
        headline = t("headline_quiet")

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


def _text(en: bool):
    """返回一个按语言取文案的小函数。

    待办的句子很短，直接放在这里比塞进 700 条的 i18n 目录更好读，
    也保证两种语言永远同时存在、不会一边漏。
    """
    zh = {
        "stopped_title": "{name} 已跌破止损 {stop:.2f}",
        "stopped_body_price": "现价 {price:.2f}，持仓 {qty} 股。按计划现在就该卖出。",
        "stopped_body": "价格已跌破止损位，按计划应当卖出。",
        "near_stop_title": "{name} 接近止损 {stop:.2f}",
        "near_stop_body_price": "现价 {price:.2f}，距止损只剩 {pct:.2f}%。提前想好到价就走，不要临场改主意。",
        "near_stop_body": "已经贴近止损位。",
        "target_title": "{name} 到达目标价 {target:.2f}",
        "near_target_title": "{name} 接近目标价 {target:.2f}",
        "target_body": "计划里的目标已到。常见做法是先卖一半，剩下用移动止损跟。",
        "expiring_title": "{name} 的建议快过期了",
        "expiring_body": "入场区间 {entry:.3f} 附近，还剩约 {hours:.0f} 小时。过期后价格条件可能已经变了。",
        "pending_title": "有 {n} 条建议在有效期内",
        "pending_body": "不着急，可以先看清楚再决定。今天不交易也是正确答案。",
        "unpriced_title": "{n} 笔持仓没有现价",
        "unpriced_body": "去行情页刷新一次，浮动盈亏和止损距离才算得准。",
        "start_title": "先跑一次扫描，看看有没有值得做的",
        "start_body": "扫描会按你的周期和本金，从候选池里挑出满足条件的标的，并算好止损、目标和股数。",
        "headline_urgent": "有 {n} 件事需要现在处理",
        "headline_soft": "有几件事值得看一眼，但都不急",
        "headline_quiet": "今天不用动手",
        "sell": "记录卖出",
        "look": "看一下",
        "handle": "现在处理",
        "see_advice": "看建议",
        "refresh": "刷行情",
        "start_action": "去扫描",
        "tag_stop": "止损",
        "tag_risk": "风险",
        "tag_profit": "止盈",
        "tag_advice": "建议",
        "tag_data": "数据",
        "tag_start": "开始",
    }
    strings = zh if not en else {
        "stopped_title": "{name} is below the stop at {stop:.2f}",
        "stopped_body_price": "Price {price:.2f}, holding {qty} shares. The plan says sell now.",
        "stopped_body": "Price is below the stop. The plan says sell.",
        "near_stop_title": "{name} is close to the stop at {stop:.2f}",
        "near_stop_body_price": "Price {price:.2f}, only {pct:.2f}% above the stop. Decide now, not in the moment.",
        "near_stop_body": "Right on top of the stop.",
        "target_title": "{name} reached the target at {target:.2f}",
        "near_target_title": "{name} is near the target at {target:.2f}",
        "target_body": "The plan's target is here. Sell at least half and trail the rest.",
        "expiring_title": "{name}'s advice expires soon",
        "expiring_body": "Entry around {entry:.3f}, about {hours:.0f} hours left. After that the price context is stale.",
        "pending_title": "{n} advice items are still valid",
        "pending_body": "No rush. Read them properly. Not trading today is a valid answer.",
        "unpriced_title": "{n} positions have no quote",
        "unpriced_body": "Refresh the market page so P&L and distance-to-stop are real.",
        "start_title": "Run a scan and see whether anything is worth doing",
        "start_body": "It screens the candidate pool against your horizon and capital, then sizes the stop, target and shares.",
        "headline_urgent": "{n} things need attention now",
        "headline_soft": "A few things worth a look, none urgent",
        "headline_quiet": "Nothing to do today",
        "sell": "Record sell",
        "look": "Look",
        "handle": "Handle now",
        "see_advice": "See advice",
        "refresh": "Refresh",
        "start_action": "Run scan",
        "tag_stop": "stop",
        "tag_risk": "risk",
        "tag_profit": "target",
        "tag_advice": "advice",
        "tag_data": "data",
        "tag_start": "start",
    }

    def get(key: str, **kwargs) -> str:
        template = strings.get(key, key)
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError):
            return template

    return get
