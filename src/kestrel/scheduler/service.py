"""Background scheduler: periodic scans, plan expiry, notification dispatch."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from kestrel.db.engine import connect
from kestrel.notify import dispatcher
from kestrel.services import config as config_service
from kestrel.services import scan as scan_service

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


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


def start() -> AsyncIOScheduler | None:
    global _scheduler
    conn = connect()
    try:
        enabled = bool(config_service.get(conn, "scan.auto_enabled", False))
        minutes = int(config_service.get(conn, "scan.interval_minutes", 60) or 60)
    finally:
        conn.close()
    if not enabled:
        log.info("auto scan disabled")
        return None
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _scan_cycle,
        IntervalTrigger(minutes=max(5, minutes)),
        id="scan_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info("auto scan every %s minutes", minutes)
    return scheduler


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
