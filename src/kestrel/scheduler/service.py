"""Background scheduler: periodic scans, position watches, dispatch.

Three jobs, deliberately separate:

* ``scan_cycle``  — produce advice. Expensive and opt-in (``scan.auto_enabled``).
* ``watch_cycle`` — look after positions you already hold. Cheap, reads cached
  quotes only, and always runs: a missed stop alert costs far more than a scan.
* ``dispatch``    — flush the notification outbox.

They share one scheduler because the app is one process, but they do not share a
schedule: a scan every hour is fine, a stop check every hour is too slow.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from kestrel.db.engine import connect
from kestrel.notify import dispatcher
from kestrel.services import config as config_service
from kestrel.services import positions as position_service
from kestrel.services import scan as scan_service

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
WATCH_MINUTES = 15


async def _scan_cycle() -> None:
    try:
        result = await scan_service.run_scan(trigger="schedule")
        if result.get("plans"):
            for rec in scan_service.active_recommendations(int(result["plans"])):
                dispatcher.queue(rec["id"])
        await dispatcher.dispatch()
        scan_service.expire_stale()
        log.info("scheduled scan: %s", {k: result.get(k) for k in ("scanned", "plans")})
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled scan failed: %s", exc)


async def _watch_cycle() -> None:
    """持倉風險檢查：跌破止損、接近止損、到達目標。

    不抓網絡：只讀 ``quote_latest``（行情頁點一次刷新就會更新）。
    這樣即使用戶沒開自動掃描，只要他看過行情，提醒就是基於真實價格的。
    """
    try:
        alerts = position_service.sweep_alerts()
        if not alerts:
            return
        for item in alerts:
            text = position_service.ALERT_TEXT[item["state"]]
            title = f"【{item['name']}】{text['title']}"
            if item["price"]:
                title += f" {item['price']:.3f}"
            body = text["body"]
            if item.get("pnl") is not None:
                body += f"\n浮动盈亏 {item['pnl']:+.0f} {item['currency']}，持仓 {item['qty']} 股。"
            dispatcher.queue_text(
                title, body, url=_absolute(f"/stock/{item['market']}/{item['code']}")
            )
        await dispatcher.dispatch()
        await dispatcher.dispatch_outbox()
        log.info("position watch: %d alerts", len(alerts))
    except Exception as exc:  # noqa: BLE001
        log.warning("position watch failed: %s", exc)


def _absolute(path: str) -> str:
    """推送到手机上的链接必须是完整地址，否则点不开。"""
    from kestrel.services import config as config_service

    conn = connect()
    try:
        base = str(config_service.get(conn, "receipt.public_base", "") or "").rstrip("/")
    finally:
        conn.close()
    return f"{base}{path}" if base else path


def start() -> AsyncIOScheduler | None:
    global _scheduler
    conn = connect()
    try:
        enabled = bool(config_service.get(conn, "scan.auto_enabled", False))
        minutes = int(config_service.get(conn, "scan.interval_minutes", 60) or 60)
    finally:
        conn.close()

    scheduler = AsyncIOScheduler()
    # 盯盘不看 auto_enabled：自动扫描关掉的人，照样需要止损提醒。
    scheduler.add_job(
        _watch_cycle,
        IntervalTrigger(minutes=WATCH_MINUTES),
        id="watch_cycle",
        max_instances=1,
        coalesce=True,
    )
    if enabled:
        scheduler.add_job(
            _scan_cycle,
            IntervalTrigger(minutes=max(5, minutes)),
            id="scan_cycle",
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    _scheduler = scheduler
    log.info(
        "scheduler started: watch every %s min, scan %s",
        WATCH_MINUTES,
        f"every {minutes} min" if enabled else "disabled",
    )
    return scheduler


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
