"""Notification dispatcher and message rendering."""

from __future__ import annotations

import json
import logging

from kestrel.core.money import format_amount
from kestrel.db.engine import connect
from kestrel.notify.channels import ChannelError, Message, build_channel
from kestrel.receipts import tokens
from kestrel.services import config as config_service

log = logging.getLogger(__name__)


def render(rec: dict, locale: str, base_url: str) -> Message:
    code = tokens.make_code(rec["id"])
    url = f"{base_url.rstrip('/')}/r/{code}" if base_url else ""
    badge = {"CN": "A股", "HK": "港股", "US": "美股"}.get(rec["market"], rec["market"])
    entry = format_amount(rec["entry_low"], 3)
    stop = format_amount(rec["stop"], 3)
    target = format_amount(rec["target1"], 3)

    if locale == "en":
        title = f"[{badge}] {rec['code']} {rec['name']}"
        body = (
            f"Entry {entry} | Stop {stop} | Target {target} | Qty {rec['qty']}\n"
            f"R:R {rec['r_multiple']}:1 · confidence {rec['confidence']:.0%}\n"
            f"Valid until {rec['valid_until'][:16]} UTC\n"
            f"{rec['explain_en'] or ''}"
        )
    else:
        title = f"【{badge}】{rec['code']} {rec['name']}"
        body = (
            f"入场 {entry}｜止损 {stop}｜目标 {target}｜{rec['qty']} 股\n"
            f"盈亏比 {rec['r_multiple']}:1 · 信心 {rec['confidence']:.0%}\n"
            f"有效期至 {rec['valid_until'][:16]} UTC\n"
            f"{rec['explain_zh'] or ''}"
        )
        if url:
            body += "\n点链接回执：买没买、成交多少"
    return Message(title=title, body=body, url=url)


def queue(rec_id: int, locale: str = "zh-CN") -> int:
    """Create pending notifications for every configured channel."""
    conn = connect()
    try:
        cfg = config_service.all_values(conn)
        channels = [c for c in cfg.get("notify.channels", []) if c.get("enabled")]
        rec = conn.execute("SELECT * FROM recommendation WHERE id=?", (rec_id,)).fetchone()
        if not rec or not channels:
            return 0
        base = cfg.get("receipt.public_base") or ""
        message = render(dict(rec), locale, base)
        count = 0
        for channel in channels:
            conn.execute(
                """
                INSERT OR IGNORE INTO notification (rec_id, channel, locale, payload)
                VALUES (?,?,?,?)
                """,
                (rec_id, channel["code"], locale, json.dumps({
                    "title": message.title, "body": message.body, "url": message.url,
                }, ensure_ascii=False)),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


async def dispatch(limit: int = 20) -> dict:
    """Send pending notifications, marking failures for retry."""
    conn = connect()
    sent = failed = 0
    try:
        cfg = config_service.all_values(conn)
        channel_cfg = {c["code"]: c for c in cfg.get("notify.channels", [])}
        rows = conn.execute(
            "SELECT * FROM notification WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        for row in rows:
            code = row["channel"]
            conf = channel_cfg.get(code)
            if not conf:
                conn.execute("UPDATE notification SET status='skipped' WHERE id=?", (row["id"],))
                continue
            channel = build_channel(code, conf)
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
                "UPDATE notification SET status='sent', sent_at=datetime('now'), attempts=attempts+1"
                " WHERE id=?",
                (row["id"],),
            )
        conn.commit()
        return {"sent": sent, "failed": failed}
    finally:
        conn.close()
