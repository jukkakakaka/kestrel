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
from decimal import Decimal

from kestrel.core.money import to_micro
from kestrel.core.quantity import db_value
from kestrel.db.engine import connect
from kestrel.indicators.engine import Series, compute
from kestrel.planning import builder
from kestrel.receipts import tokens
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import journal as journal_service
from kestrel.services import rules as rule_service
from kestrel.services import timeframe
from kestrel.services import universe as universe_service

log = logging.getLogger(__name__)

MAX_CONCURRENT_FETCH = 6
MIN_BARS = 40

ACTION_BUY = "buy"
ACTION_WATCH = "watch"
ACTION_AVOID = "avoid"


def _headline(action: str, plan, ind: dict, name: str, locale: str = "zh-CN") -> tuple[str, str]:
    """一句话结论，中英各一份。

    库里的 headline 是单列，谁先写入就是谁 —— 英文用户会看到中文句子。
    这里同时生成两种，模板按当前语言取，避免再出现「英文界面夹中文」。
    """
    if plan is None:
        return (
            "现在不需要动手，等条件变好。",
            "Nothing to do right now; wait for the setup to improve.",
        )
    return (
        f"在 {plan.entry_low:.2f}–{plan.entry_high:.2f} 之间买入，"
        f"跌破 {plan.stop:.2f} 卖出认错，先看 {plan.target1:.2f}。",
        f"Buy between {plan.entry_low:.2f} and {plan.entry_high:.2f}; "
        f"exit below {plan.stop:.2f}; first target {plan.target1:.2f}.",
    )


STRATEGY_NAME = {
    "trend_pullback": "趋势回踩",
    "breakout_20": "20 日突破",
    "mean_reversion": "超跌反弹",
}
STRATEGY_NAME_EN = {
    "trend_pullback": "trend pullback",
    "breakout_20": "20-day breakout",
    "mean_reversion": "oversold bounce",
}

HOLD_EN = {
    "short": "usually held for days to a few weeks",
    "medium": "usually held for weeks to a couple of months",
    "long": "usually held for months, managed on monthly bars",
}

_ROMAN = ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ")


def _sector_key(sector: str) -> str:
    """Eastmoney splits one industry into 银行 and 银行Ⅱ; treat them as one."""
    text = (sector or "").strip()
    while text and text[-1] in _ROMAN:
        text = text[:-1]
    return text


HOLD_ZH = {
    "short": "通常持有几天到几周",
    "medium": "通常持有几周到两个月",
    "long": "通常持有几个月，按月线级别管理",
}


def _learn(plan, ind: dict, signal, risk_share: float, horizon: str = "short") -> tuple[str, str]:
    """学习模式的解释，中英各一份。"""
    if plan is None:
        return (
            "这是什么：现在不需要动手。\n"
            "为什么不做：价格形态或风险回报不满足进场条件，硬做等于赌。\n"
            "如果硬做会怎样：止损位不好找，亏的钱会比赚的多。",
            "What this is: nothing to do right now.\n"
            "Why not: the setup or the reward does not clear the entry rules; forcing it is gambling.\n"
            "If forced: the stop is hard to place, so the loss ends up bigger than the win.",
        )
    risk_pct = float((plan.entry_low - plan.stop) / plan.entry_low * 100)
    name = STRATEGY_NAME.get(signal.code, "当前策略")
    name_en = STRATEGY_NAME_EN.get(signal.code, signal.code)
    last = float(ind.get("last") or 0)
    zh = (
        f"这是什么：一笔做多（先买后卖）的计划，{HOLD_ZH.get(horizon, HOLD_ZH['short'])}。\n"
        f"为什么现在做：「{name}」在 {last:.2f} 附近给出信号。\n"
        f"如果错了会怎样：跌破 {plan.stop:.2f}（约 −{risk_pct:.1f}%）就卖，"
        f"这一笔最多亏 {plan.risk_amount:.2f}，占你总资金约 {risk_share:.1f}%。"
    )
    en = (
        f"What this is: a long plan, {HOLD_EN.get(horizon, HOLD_EN['short'])}.\n"
        f"Why now: “{name_en}” fired near {last:.2f}.\n"
        f"If wrong: sell below {plan.stop:.2f} (about −{risk_pct:.1f}%). "
        f"The most this trade loses is {plan.risk_amount:.2f}, about {risk_share:.1f}% of capital."
    )
    return zh, en


def _used_exposure_cny() -> Decimal:
    """Cost basis of what is already open, in CNY.

    Entry price is the only price the journal owns reliably, so exposure is
    measured at cost. That understates a winner and overstates a loser — both are
    more honest than pretending we know a live quote at scan time.
    """
    total = Decimal("0")
    try:
        for position in journal_service.positions():
            total += journal_service.to_cny(
                Decimal(str(position["entry"])) * Decimal(str(position["qty"])), position["currency"]
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read open positions: %s", exc)
    return total


def _plan_cost_cny(plan) -> Decimal:
    return journal_service.to_cny(Decimal(str(plan.entry_low)) * plan.qty, plan.currency)


def _exceeds_exposure(cost_cny: Decimal, cap: Decimal, used: Decimal, added: Decimal) -> bool:
    """Would this plan push total exposure over the ceiling?

    A zero or negative cap means "not configured" and must not block everything —
    the setting shipped with a default of 60, but an explicit 0 is a legitimate
    way of saying "no new positions" only if the user asked for it. Treating it
    as unlimited keeps the old behaviour for anyone who never opened the page.
    """
    if cap <= 0:
        return False
    return used + added + cost_cny > cap


def _resolve_horizon(settings_map: dict) -> tuple[str, dict | None]:
    """Turn ``trade.horizon`` into a concrete bar size.

    Delegates to the advisor so the scanner and the AI brief can never disagree
    about which bars the numbers were measured on.
    """
    from kestrel.services import advisor

    return advisor.resolve(settings_map.get("trade.horizon", timeframe.AUTO))


async def _load_bars(conn, symbols: list[dict], limit: int = 260) -> dict[int, list[dict]]:
    """Read cache, refresh stale symbols concurrently, then persist.

    Network calls run in parallel but every database write happens afterwards on
    one thread: a single sqlite connection shared by concurrent tasks corrupts.
    """
    cached: dict[int, list[dict]] = {}
    need: list[dict] = []
    for sym in symbols:
        rows = bars_service.cached_bars(conn, sym["id"], limit)
        cached[sym["id"]] = rows
        if bars_service.needs_fetch(conn, sym["id"], sym["market"], rows, limit):
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
                bars_service.save_bars(conn, symbol_id, rows, depth=limit)
                cached[symbol_id] = rows[-limit:]
        conn.commit()

    markets = {sym["id"]: sym["market"] for sym in symbols}
    return {
        symbol_id: bars_service.drop_forming(rows, markets.get(symbol_id))
        for symbol_id, rows in cached.items()
    }


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
        exposure_pct = float(settings_map.get("risk.max_total_exposure_pct", 60) or 60)
        horizon_code, advice = _resolve_horizon(settings_map)
        horizon = timeframe.get(horizon_code)

        cur = conn.execute(
            "INSERT INTO scan_run (trigger, markets, universe, pool_size, horizon)"
            " VALUES (?,?,?,?,?)",
            (trigger, json.dumps(markets), mode, pool_size, horizon.code),
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
        bars_map = await _load_bars(conn, symbols, limit=horizon.lookback)

        # Aggregate first, then decide who is too short: a symbol with 300 daily
        # bars is fine weekly and thin monthly, so the threshold belongs to the
        # horizon, not to the cache.
        prepared: dict[int, list[dict]] = {}
        skipped: list[dict] = []
        for sym in symbols:
            rows = bars_map.get(sym["id"]) or []
            folded = timeframe.prepare(rows, horizon.code)
            if len(folded) < horizon.min_bars:
                skipped.append(
                    {
                        "market": sym["market"],
                        "code": sym["code"],
                        "name": sym["name"],
                        "reason": "no_bars" if not rows else "short_history",
                        "bars": len(folded),
                    }
                )
                continue
            prepared[sym["id"]] = folded

        strategies = rule_service.registry()

        # A rule measured as losing money on *this* bar size may not publish.
        # Saying nothing is a feature.
        blocked = rule_service.blocked_for(horizon.code)
        allowed = {code: code not in blocked for code in strategies}

        signals = plans = 0
        near: list[dict] = []
        rejected: list[dict] = []
        blocked_lot = 0
        sector_count: dict[str, int] = {}
        max_per_sector = int(settings_map.get("risk.max_per_sector", 2))

        # Exposure is a portfolio fact, not a per-plan one: three plans each
        # risking 2% are not three independent decisions. The setting existed in
        # the UI for a while without anything reading it.
        exposure_cap = Decimal(str(capital)) * Decimal(str(exposure_pct)) / Decimal("100")
        exposure_used = _used_exposure_cny()
        exposure_added = Decimal("0")

        for sym in symbols:
            rows = prepared.get(sym["id"]) or []
            if not rows:
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
                horizon=horizon.code,
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

            # Portfolio ceiling. Checked last so the more specific rejections
            # (bad stop, sector cap) keep their explanations.
            cost_cny = _plan_cost_cny(plan)
            if _exceeds_exposure(cost_cny, exposure_cap, exposure_used, exposure_added):
                rejected.append(
                    {
                        "market": sym["market"],
                        "code": sym["code"],
                        "name": sym["name"],
                        "reason": "exposure_cap",
                        "score": round(best.score, 3),
                        "need": float(cost_cny),
                        "left": float(exposure_cap - exposure_used - exposure_added),
                    }
                )
                continue
            exposure_added += cost_cny

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
                     action, headline, learn_zh, learn_en, horizon)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',
                        ?,?,?,?,?)
                """,
                (
                    scan_id, sym["id"], plan.market, plan.code, plan.name, plan.currency,
                    plan.strategy, plan.score,
                    to_micro(plan.entry_low), to_micro(plan.entry_high),
                    to_micro(plan.stop), to_micro(plan.target1),
                    to_micro(plan.target2) if plan.target2 else None,
                    db_value(plan.qty), plan.lot_size,
                    to_micro(plan.risk_amount), to_micro(plan.est_fees),
                    to_micro(plan.breakeven), plan.r_multiple, plan.confidence,
                    plan.valid_until, json.dumps(plan.invalidation),
                    plan.explain_zh, plan.explain_en,
                    ACTION_BUY,
                    _headline(ACTION_BUY, plan, ind, sym["name"])[0],
                    _learn(plan, ind, best, risk_share, horizon.code)[0],
                    _learn(plan, ind, best, risk_share, horizon.code)[1].replace(
                        "\n", "\n"
                    ),
                    horizon.code,
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
            "horizon": {
                "code": horizon.code,
                "label": horizon.zh,
                "requested": str(settings_map.get("trade.horizon", timeframe.AUTO)),
                "auto": advice is not None,
                "reason": (advice or {}).get("primary_reason") or "",
            },
            "exposure": {
                "cap": float(exposure_cap),
                "used": float(exposure_used),
                "added": float(exposure_added),
                "pct": exposure_pct,
            },
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
