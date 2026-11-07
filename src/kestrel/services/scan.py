"""Scan orchestration: universe -> bars -> indicators -> strategies -> advice.

Three things this module is responsible for that the older version got wrong:

1. It looks at a *pool* of candidates, not just the watchlist. Scanning five
   symbols you already own is not stock picking.
2. It tells the user what to do next: buy, wait, or ignore. A price ladder with
   no verb is not advice.
3. It records what it skipped and why, so an empty result is explainable
   instead of looking broken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.indicators.engine import Series, compute
from kestrel.planning import builder
from kestrel.receipts import tokens
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import universe as universe_service
from kestrel.strategies.base import registry

log = logging.getLogger(__name__)

MAX_CONCURRENT_FETCH = 6
MIN_BARS = 40

ACTION_BUY = "buy"
ACTION_WATCH = "watch"
ACTION_AVOID = "avoid"


def _headline_zh(action: str, plan, ind: dict, name: str) -> str:
    if plan is None:
        return "现在不需要动手，等条件变好。"
    return (
        f"在 {plan.entry_low:.2f}–{plan.entry_high:.2f} 之间买入，"
        f"跌破 {plan.stop:.2f} 卖出认错，先看 {plan.target1:.2f}。"
    )


STRATEGY_NAME = {
    "trend_pullback": "趋势回踩",
    "breakout_20": "20 日突破",
    "mean_reversion": "超跌反弹",
}

_ROMAN = ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ")


def _sector_key(sector: str) -> str:
    """Eastmoney splits one industry into 银行 and 银行Ⅱ; treat them as one."""
    text = (sector or "").strip()
    while text and text[-1] in _ROMAN:
        text = text[:-1]
    return text


def _learn_zh(plan, ind: dict, signal, risk_share: float) -> str:
    if plan is None:
        return (
            "这是什么：现在不需要动手。\n"
            "为什么不做：价格形态或风险回报不满足进场条件，硬做等于赌。\n"
            "如果硬做会怎样：止损位不好找，亏的钱会比赚的多。"
        )
    risk_pct = float((plan.entry_low - plan.stop) / plan.entry_low * 100)
    name = STRATEGY_NAME.get(signal.code, "当前策略")
    return (
        "这是什么：一笔做多（先买后卖）的计划，通常持有几天到几周。\n"
        f"为什么现在做：「{name}」在 {float(ind.get('last') or 0):.2f} 附近给出信号。\n"
        f"如果错了会怎样：跌破 {plan.stop:.2f}（约 −{risk_pct:.1f}%）就卖，"
        f"这一笔最多亏 {plan.risk_amount:.2f}，占你总资金约 {risk_share:.1f}%。"
    )


async def _load_bars(conn, symbols: list[dict], limit: int = 260) -> tuple[dict, list[dict]]:
    """Read cache, refresh stale symbols concurrently, then persist.

    Network calls run in parallel but every database write happens afterwards on
    one thread: a single sqlite connection shared by concurrent tasks corrupts.
    """
    cached: dict[int, list[dict]] = {}
    need: list[dict] = []
    for sym in symbols:
        rows = bars_service.cached_bars(conn, sym["id"], limit)
        cached[sym["id"]] = rows
        if bars_service.is_stale(rows):
            need.append(sym)

    if need:
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCH)

        async def pull(sym: dict) -> tuple[int, list[dict]]:
            async with sem:
                rows = await bars_service.fetch_only(sym["market"], sym["code"], limit=limit)
                return sym["id"], rows

        results = await asyncio.gather(*(pull(s) for s in need))
        for symbol_id, rows in results:
            if rows:
                bars_service.save_bars(conn, symbol_id, rows)
                cached[symbol_id] = rows[-limit:]
        conn.commit()

    skipped: list[dict] = []
    for sym in symbols:
        rows = cached.get(sym["id"]) or []
        if len(rows) < MIN_BARS:
            skipped.append(
                {
                    "market": sym["market"],
                    "code": sym["code"],
                    "name": sym["name"],
                    "reason": "no_bars" if not rows else "short_history",
                    "bars": len(rows),
                }
            )
    return cached, skipped


async def _resolve_missing(symbols: list[dict]) -> None:
    from kestrel.datasources import resolve as resolver

    for sym in symbols:
        if sym.get("secid"):
            continue
        found = await resolver.resolve(sym["code"], sym["market"])
        if not found:
            continue
        conn = connect()
        try:
            conn.execute(
                "UPDATE symbol SET secid=?, name=?, exchange=?, resolve_error=NULL WHERE id=?",
                (found.secid, found.name, found.exchange, sym["id"]),
            )
            conn.commit()
        finally:
            conn.close()


def _pick_symbols(markets: list[str], mode: str, pool_size: int) -> list[dict]:
    conn = connect()
    try:
        marks = ",".join("?" * len(markets))
        watch = conn.execute(
            f"""
            SELECT s.id, s.market, s.code, s.name, s.currency, s.lot_size, s.secid
            FROM watchlist w JOIN symbol s ON s.id = w.symbol_id
            WHERE s.market IN ({marks})
            """,
            tuple(markets),
        ).fetchall()
        rows = [dict(r) for r in watch]
        if mode in ("pool", "both"):
            seen = {(r["market"], r["code"]) for r in rows}
            for item in universe_service.pool_symbols(markets, pool_size):
                if (item["market"], item["code"]) in seen:
                    continue
                rows.append(item)
        return rows
    finally:
        conn.close()


async def run_scan(trigger: str = "manual", markets: list[str] | None = None) -> dict:
    started = time.perf_counter()
    conn = connect()
    scan_id = 0
    try:
        settings_map = config_service.all_values(conn)
        markets = markets or settings_map.get("scan.markets") or ["CN", "HK", "US"]
        min_score = float(settings_map.get("scan.min_score", 0.55))
        capital = float(settings_map.get("account.capital", 10000))
        risk_pct = float(settings_map.get("risk.per_trade_pct", 2.0))
        mode = str(settings_map.get("scan.universe", "both"))
        pool_size = int(settings_map.get("scan.pool_size", 30))

        cur = conn.execute(
            "INSERT INTO scan_run (trigger, markets, universe, pool_size) VALUES (?,?,?,?)",
            (trigger, json.dumps(markets), mode, pool_size),
        )
        scan_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    pool_counts: dict[str, int] = {}
    if mode in ("pool", "both"):
        try:
            pool_counts = await universe_service.refresh(markets, pool_size)
        except Exception as exc:  # noqa: BLE001
            log.warning("pool refresh failed: %s", exc)

    conn = connect()
    try:
        symbols = _pick_symbols(markets, mode, pool_size)
        await _resolve_missing(symbols)
        bars_map, skipped = await _load_bars(conn, symbols)

        strategies = registry()
        # Only strategies that have demonstrated positive expectancy across the
        # universe may publish advice. A rule that backtests as a loser does not
        # become a good idea because the chart looks nice today.
        from kestrel.services import edge as edge_service

        allowed = edge_service.enabled_strategies()
        signals = plans = 0
        near: list[dict] = []
        rejected: list[dict] = []
        blocked_lot = 0
        sector_count: dict[str, int] = {}
        max_per_sector = int(settings_map.get("risk.max_per_sector", 2))

        for sym in symbols:
            rows = bars_map.get(sym["id"]) or []
            if len(rows) < MIN_BARS:
                continue
            ind = compute(Series.from_bars(rows))
            if not ind:
                continue

            best = None
            best_any = None
            for strategy in strategies.values():
                if not allowed.get(strategy.code, True):
                    continue
                result = strategy.evaluate(ind)
                if result is None:
                    continue
                if best_any is None or result.score > best_any.score:
                    best_any = result
                if result.score >= min_score and (best is None or result.score > best.score):
                    best = result

            if best is None:
                if best_any is not None and best_any.score >= min_score * 0.75:
                    near.append(
                        {
                            "code": sym["code"],
                            "name": sym["name"],
                            "market": sym["market"],
                            "strategy": best_any.code,
                            "score": round(best_any.score, 3),
                        }
                    )
                continue
            signals += 1

            broker = settings_map.get(f"broker.{sym['market']}")
            plan, why = builder.build_ex(
                market=sym["market"],
                code=sym["code"],
                name=sym["name"],
                ind=ind,
                signal=best,
                capital_cny=capital,
                risk_pct=risk_pct,
                lot_size=sym["lot_size"] or 1,
                broker=broker,
            )
            if plan is None:
                if why == "lot_too_big":
                    blocked_lot += 1
                rejected.append(
                    {
                        "market": sym["market"],
                        "code": sym["code"],
                        "name": sym["name"],
                        "reason": why or "unknown",
                        "score": round(best.score, 3),
                    }
                )
                continue

            # Concentration guard: a screen that returns eight banks is a sector
            # bet wearing a stock picker's clothes.
            sector = _sector_key(sym.get("sector") or "")
            if sector and sector_count.get(sector, 0) >= max_per_sector:
                rejected.append(
                    {
                        "market": sym["market"],
                        "code": sym["code"],
                        "name": sym["name"],
                        "reason": "sector_cap",
                        "score": round(best.score, 3),
                        "sector": sector,
                    }
                )
                continue
            if sector:
                sector_count[sector] = sector_count.get(sector, 0) + 1

            plans += 1

            risk_share = float(plan.risk_amount) / capital * 100 if capital else 0.0
            conn.execute(
                "INSERT INTO signal (scan_id, symbol_id, strategy, score, reasons)"
                " VALUES (?,?,?,?,?)",
                (scan_id, sym["id"], best.code, best.score, json.dumps(best.reasons)),
            )
            conn.execute(
                "UPDATE recommendation SET status='superseded'"
                " WHERE symbol_id=? AND status='active'",
                (sym["id"],),
            )
            cur = conn.execute(
                """
                INSERT INTO recommendation
                    (scan_id, symbol_id, market, code, name, currency, strategy, score,
                     entry_low, entry_high, stop, target1, target2, qty, lot_size,
                     risk_amount, est_fees, breakeven, r_multiple, confidence,
                     valid_until, invalidation, explain_zh, explain_en, status,
                     action, headline, learn_zh, learn_en)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',
                        ?,?,?,?)
                """,
                (
                    scan_id, sym["id"], plan.market, plan.code, plan.name, plan.currency,
                    plan.strategy, plan.score,
                    to_micro(plan.entry_low), to_micro(plan.entry_high),
                    to_micro(plan.stop), to_micro(plan.target1),
                    to_micro(plan.target2) if plan.target2 else None,
                    plan.qty, plan.lot_size,
                    to_micro(plan.risk_amount), to_micro(plan.est_fees),
                    to_micro(plan.breakeven), plan.r_multiple, plan.confidence,
                    plan.valid_until, json.dumps(plan.invalidation),
                    plan.explain_zh, plan.explain_en,
                    ACTION_BUY,
                    _headline_zh(ACTION_BUY, plan, ind, sym["name"]),
                    _learn_zh(plan, ind, best, risk_share),
                    "",
                ),
            )
            rec_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO receipt_token (code, rec_id, expires_at)"
                " VALUES (?,?,?)",
                (tokens.make_code(rec_id), rec_id, tokens.expiry()),
            )

        duration = int((time.perf_counter() - started) * 1000)
        # The page must reflect the newest scan, not a pile of older runs. Any
        # advice for a symbol this scan looked at but no longer likes is retired.
        conn.execute(
            """
            UPDATE recommendation SET status='superseded'
            WHERE status='active' AND scan_id <> ?
              AND symbol_id IN (SELECT id FROM symbol WHERE is_pool=1)
            """,
            (scan_id,),
        )
        conn.execute(
            "UPDATE scan_run SET finished_at=?, scanned=?, signals=?, plans=?, status='ok',"
            " skipped=?, rejected=?, duration_ms=? WHERE id=?",
            (
                datetime.now(UTC).isoformat(timespec="seconds"),
                len(symbols) - len(skipped),
                signals,
                plans,
                json.dumps(skipped),
                json.dumps(rejected),
                duration,
                scan_id,
            ),
        )
        conn.commit()

        summary = {
            "ok": True,
            "scan_id": scan_id,
            "total": len(symbols),
            "scanned": len(symbols) - len(skipped),
            "signals": signals,
            "plans": plans,
            "blocked_lot": blocked_lot,
            "skipped": skipped,
            "rejected": rejected,
            "near": sorted(near, key=lambda x: -x["score"])[:8],
            "min_score": min_score,
            "universe": mode,
            "pool": pool_counts,
            "allowed": allowed,
            "duration_ms": duration,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        config_service.put(conn, "scan.last", summary)
        conn.commit()
        return summary
    except Exception as exc:  # noqa: BLE001
        log.exception("scan failed")
        try:
            conn.execute(
                "UPDATE scan_run SET finished_at=?, status='failed', error=? WHERE id=?",
                (datetime.now(UTC).isoformat(timespec="seconds"), str(exc), scan_id),
            )
            conn.commit()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def active_recommendations(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT r.*, s.sector AS sector
            FROM recommendation r LEFT JOIN symbol s ON s.id = r.symbol_id
            WHERE r.status='active'
               OR (r.status IN ('executed','skipped') AND r.decided_at >= date('now','-2 day'))
            ORDER BY CASE r.status WHEN 'active' THEN 0 ELSE 1 END,
                     r.created_at DESC, r.score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def history(limit: int = 40) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT r.*, s.sector AS sector
            FROM recommendation r LEFT JOIN symbol s ON s.id = r.symbol_id
            WHERE r.status IN ('executed','skipped','expired','superseded')
            ORDER BY r.created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def decide(rec_id: int, decision: str, note: str = "", fill_price: float | None = None) -> bool:
    """Record what the user actually did with an advice card."""
    status = {"executed": "executed", "skipped": "skipped", "snooze": "active"}.get(decision)
    if status is None:
        return False
    conn = connect()
    try:
        conn.execute(
            "UPDATE recommendation SET status=?, decided_at=?, decided_note=?, fill_price=?"
            " WHERE id=?",
            (
                status,
                datetime.now(UTC).isoformat(timespec="seconds"),
                note,
                to_micro(fill_price) if fill_price else None,
                rec_id,
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def expire_stale() -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE recommendation SET status='expired'"
            " WHERE status='active' AND valid_until < ?",
            (datetime.now(UTC).isoformat(timespec="seconds"),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def recent_scans(limit: int = 10) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM scan_run ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("skipped", "rejected"):
                try:
                    item[key] = json.loads(item[key]) if item.get(key) else []
                except (TypeError, ValueError):
                    item[key] = []
            out.append(item)
        return out
    finally:
        conn.close()
