"""Notification dispatcher and message rendering."""

from __future__ import annotations

import json
import logging

from kestrel.core.money import format_amount
from kestrel.core.quantity import display
from kestrel.db.engine import connect
from kestrel.notify.channels import ChannelError, Message, build_channel
from kestrel.receipts import tokens
from kestrel.services import settings_schema

log = logging.getLogger(__name__)

CHANNEL_KEYS = ("workbuddy", "weclawbot", "ntfy", "webhook", "smtp")


async def send_test(channel: str | None = None) -> dict:
    """Send one message and report exactly what happened, per channel.

    A settings page without a test button is a guessing game: the user cannot
    tell "configured" from "working".
    """
    from kestrel.services import channels as channel_service

    conn = connect()
    try:
        cfg = settings_schema.read_all(conn)
        active = dict(configured_channels(cfg))
    finally:
        conn.close()

    targets = [channel] if channel else list(active.keys())
    if not targets:
        return {
            "ok": False,
            "error": "还没有启用任何推送渠道。先在下面填好并打开开关。",
            "results": [],
        }

    message = Message(
        title="Kestrel 测试消息",
        body="如果你看到这条，说明推送链路是通的。这条不是交易建议。",
        url="",
    )

    results = []
    for code in targets:
        conf = active.get(code)
        if conf is None:
            conf = channel_service.config_for(code)
            if not conf:
                results.append({"channel": code, "ok": False, "error": "未配置"})
                continue
        try:
            impl = build_channel(code, conf)
            if impl is None:
                results.append({"channel": code, "ok": False, "error": "未知渠道"})
                continue
            await impl.send(message)
            channel_service.mark_result(code, ok=True)
            results.append({"channel": code, "ok": True})
        except Exception as exc:  # noqa: BLE001
            channel_service.mark_result(code, ok=False, error=str(exc))
            results.append({"channel": code, "ok": False, "error": str(exc)[:200]})

    return {"ok": any(r["ok"] for r in results), "results": results}


def configured_channels(cfg: dict | None = None) -> list[tuple[str, dict]]:
    """当前真正启用的渠道。

    推送渠道配置存在 ``notify_channel`` 表里（设置页写的是这张表），
    而不是 ``config_kv``（那些 ``notify.*`` 键是早期版本留下的壳，从未被读取）。
    这里统一走 ``services.channels``，否则会出现「设置页显示已启用、实际一条
    也发不出去」——这是最坏的一种失败：用户以为提醒在跑。
    """
    from kestrel.services import channels as channel_service

    return channel_service.enabled_channels()


def render(rec: dict, locale: str, base_url: str) -> Message:
    code = tokens.make_code(rec["id"])
    url = f"{base_url.rstrip('/')}/r/{code}" if base_url else ""
    badge = {"CN": "A股", "HK": "港股", "US": "美股"}.get(rec["market"], rec["market"])
    entry = format_amount(rec["entry_low"], 3)
    stop = format_amount(rec["stop"], 3)
    target = format_amount(rec["target1"], 3)
    qty = display(rec["qty"])

    if locale == "en":
        title = f"[{badge}] {rec['code']} {rec['name']}"
        body = (
            f"Entry {entry} | Stop {stop} | Target {target} | Qty {qty}\n"
            f"R:R {rec['r_multiple']}:1 · confidence {rec['confidence']:.0%}\n"
            f"Valid until {str(rec['valid_until'])[:16]}Z\n{rec.get('explain_en') or ''}"
        )
    else:
        title = f"【{badge}】{rec['code']} {rec['name']}"
        body = (
            f"入场 {entry}｜止损 {stop}｜目标 {target}｜{qty} 股\n"
            f"盈亏比 {rec['r_multiple']}:1 · 信心 {rec['confidence']:.0%}\n"
            f"有效期至 {str(rec['valid_until'])[:16]}Z\n{rec.get('explain_zh') or ''}"
        )
        if url:
            body += "\n点链接回执：买没买、成交多少"
    return Message(title=title, body=body, url=url)


def queue(rec_id: int, locale: str = "zh-CN") -> int:
    conn = connect()
    try:
        cfg = settings_schema.read_all(conn)
        channels = configured_channels(cfg)
        rec = conn.execute("SELECT * FROM recommendation WHERE id=?", (rec_id,)).fetchone()
        if not rec or not channels:
            return 0
        message = render(dict(rec), locale, cfg.get("receipt.public_base") or "")
        count = 0
        for code, _ in channels:
            conn.execute(
                "INSERT OR IGNORE INTO notification (rec_id, channel, locale, payload)"
                " VALUES (?,?,?,?)",
                (rec_id, code, locale, json.dumps(
                    {"title": message.title, "body": message.body, "url": message.url},
                    ensure_ascii=False,
                )),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


async def dispatch(limit: int = 20) -> dict:
    conn = connect()
    sent = failed = 0
    try:
        cfg = settings_schema.read_all(conn)
        channels = dict(configured_channels(cfg))
        rows = conn.execute(
            "SELECT * FROM notification WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        for row in rows:
            code = row["channel"]
            conf = channels.get(code)
            channel = build_channel(code, conf) if conf else None
            if channel is None:
                conn.execute("UPDATE notification SET status='skipped' WHERE id=?", (row["id"],))
                continue
            payload = json.loads(row["payload"])
            message = Message(payload["title"], payload["body"], payload.get("url", ""))
            try:
                await channel.send(message)
            except (ChannelError, Exception) as exc:  # noqa: BLE001
                failed += 1
                conn.execute(
                    "UPDATE notification SET attempts=attempts+1, last_error=? WHERE id=?",
                    (str(exc)[:300], row["id"]),
                )
                log.warning("notify %s failed: %s", code, exc)
                continue
            sent += 1
            conn.execute(
                "UPDATE notification SET status='sent', sent_at=datetime('now'),"
                " attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
        conn.commit()
        return {"sent": sent, "failed": failed}
    finally:
        conn.close()


def queue_text(
    title: str,
    body: str,
    url: str = "",
    priority: str = "default",
    locale: str = "zh-CN",
) -> int:
    """把一条不属于任何建议的消息放进发件箱。

    持仓止损提醒必须能被单独发送：它不依附于某个 recommendation，
    但和它一样需要「失败了下次还能重试」的语义。
    """
    conn = connect()
    try:
        cfg = settings_schema.read_all(conn)
        channels = configured_channels(cfg)
        if not channels:
            return 0
        count = 0
        for code, _ in channels:
            conn.execute(
                "INSERT INTO message_outbox (channel, title, body, url, priority)"
                " VALUES (?,?,?,?,?)",
                (code, title, body, url or None, priority),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


async def dispatch_outbox(limit: int = 20) -> dict:
    """发送 queue_text 写入的消息，成功后标记 sent。"""
    conn = connect()
    sent = failed = 0
    try:
        cfg = settings_schema.read_all(conn)
        channels = dict(configured_channels(cfg))
        rows = conn.execute(
            "SELECT * FROM message_outbox WHERE status='pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        for row in rows:
            code = row["channel"]
            conf = channels.get(code)
            channel = build_channel(code, conf) if conf else None
            if channel is None:
                conn.execute(
                    "UPDATE message_outbox SET status='skipped' WHERE id=?", (row["id"],)
                )
                continue
            message = Message(row["title"], row["body"], row["url"] or "",
                              row["priority"] or "default")
            try:
                await channel.send(message)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                conn.execute(
                    "UPDATE message_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                    (str(exc)[:300], row["id"]),
                )
                log.warning("outbox %s failed: %s", code, exc)
                continue
            sent += 1
            conn.execute(
                "UPDATE message_outbox SET status='sent', sent_at=datetime('now'),"
                " attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
        conn.commit()
        return {"sent": sent, "failed": failed}
    finally:
        conn.close()
