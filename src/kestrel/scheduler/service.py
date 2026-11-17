"""后台定时调度器：定时生成建议、持仓盯盘刷新与止损通知、版本检查。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from kestrel.core.clock import iso
from kestrel.core.money import to_micro
from kestrel.datasources.chain import default_chain
from kestrel.db.engine import connect, transaction
from kestrel.notify import dispatcher
from kestrel.services import config as config_service
from kestrel.services import engine as engine_service
from kestrel.services import positions as position_service
from kestrel.services import scan as scan_service

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
WATCH_MINUTES = 15


def _save_watch_quotes(quotes: dict) -> None:
    """将盯盘拉取到的持仓最新行情写入 ``quote_latest``。"""
    if not quotes:
        return
    conn = connect()
    try:
        with transaction(conn):
            for q in quotes.values():
                row = conn.execute(
                    "SELECT id FROM symbol WHERE market=? AND code=?",
                    (q.market, q.code),
                ).fetchone()
                if not row:
                    continue
                conn.execute(
                    """
                    INSERT INTO quote_latest
                        (symbol_id, price, prev_close, open, high, low, volume, turnover,
                         change_pct, source, quoted_at, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(symbol_id) DO UPDATE SET
                        price=excluded.price, prev_close=excluded.prev_close, open=excluded.open,
                        high=excluded.high, low=excluded.low, volume=excluded.volume,
                        turnover=excluded.turnover, change_pct=excluded.change_pct,
                        source=excluded.source, quoted_at=excluded.quoted_at,
                        fetched_at=excluded.fetched_at
                    """,
                    (
                        row["id"],
                        to_micro(q.price),
                        to_micro(q.prev_close) if q.prev_close else None,
                        to_micro(q.open) if q.open else None,
                        to_micro(q.high) if q.high else None,
                        to_micro(q.low) if q.low else None,
                        q.volume,
                        to_micro(q.turnover) if q.turnover else None,
                        q.change_pct,
                        q.source,
                        iso(q.quoted_at),
                    ),
                )
    finally:
        conn.close()


async def _scan_cycle() -> None:
    """按用户当前配置的引擎（规则 / 大模型 / WorkBuddy）执行定时扫描并推送新建议。"""
    try:
        result = await engine_service.run(trigger="schedule")
        scan_service.expire_stale()
        log.info(
            "scheduled scan (%s): %s",
            result.get("engine"),
            {k: result.get(k) for k in ("scanned", "plans", "accepted")},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduled scan failed: %s", exc)


async def _update_cycle() -> None:
    """每天两次检查 GitHub Release 新版本，仅提醒不静默覆盖安装。"""
    try:
        from kestrel.services import updates

        if not updates.auto_enabled():
            return
        report = await updates.check()
        if not report.get("available"):
            return
        latest = report.get("latest") or {}
        dispatcher.queue_text(
            f"Kestrel 有新版本 v{latest.get('version')}",
            f"当前 v{report.get('current')}。到「设置 → 关于」点一下就能下载并安装。",
            url=_absolute("/settings#about"),
        )
        await dispatcher.dispatch_outbox()
        log.info("update available: %s", latest.get("version"))
    except Exception as exc:  # noqa: BLE001
        log.warning("update check failed: %s", exc)


async def _watch_cycle() -> None:
    """持仓风控巡检：先尝试刷新持仓股票最新现价，再执行止损/止盈预警判定。"""
    try:
        conn = connect()
        try:
            open_rows = conn.execute(
                "SELECT DISTINCT market, code FROM trade WHERE status='open'"
            ).fetchall()
        finally:
            conn.close()

        pairs = [(r["market"], r["code"]) for r in open_rows]
        if pairs:
            try:
                quotes = await default_chain().fetch(pairs)
                if quotes:
                    _save_watch_quotes(quotes)
            except Exception as exc:  # noqa: BLE001
                log.debug("watch quote refresh skipped: %s", exc)

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
    conn = connect()
    try:
        base = str(config_service.get(conn, "receipt.public_base", "") or "").rstrip("/")
    finally:
        conn.close()
    return f"{base}{path}" if base else path


def reschedule() -> None:
    """在设置页修改自动扫描开关或频率后，无需重启立即热更新调度计划。"""
    if _scheduler is None:
        return
    conn = connect()
    try:
        enabled = bool(config_service.get(conn, "scan.auto_enabled", False))
        minutes = int(config_service.get(conn, "scan.interval_minutes", 60) or 60)
    finally:
        conn.close()

    if _scheduler.get_job("scan_cycle") is not None:
        _scheduler.remove_job("scan_cycle")
    if enabled:
        _scheduler.add_job(
            _scan_cycle,
            IntervalTrigger(minutes=max(5, minutes)),
            id="scan_cycle",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    log.info("scheduler rescheduled: scan %s", f"every {minutes} min" if enabled else "disabled")


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
    scheduler.add_job(
        _update_cycle,
        IntervalTrigger(hours=12),
        id="update_cycle",
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
