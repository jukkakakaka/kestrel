"""扫描编排：候选池 -> K 线缓存 -> 指标与市场环境 -> 策略筛选 -> 风控与仓位计划 -> 建议入库。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from kestrel.core.money import to_cny, to_micro
from kestrel.core.quantity import db_value
from kestrel.db.engine import connect
from kestrel.indicators.engine import Series, compute_pair
from kestrel.planning import builder
from kestrel.receipts import tokens
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import journal as journal_service
from kestrel.services import portfolio as portfolio_service
from kestrel.services import rules as rule_service
from kestrel.services import timeframe
from kestrel.services import universe as universe_service
from kestrel.strategies import library as strategy_library

log = logging.getLogger(__name__)

MAX_CONCURRENT_FETCH = 6
MIN_BARS = 40

ACTION_BUY = "buy"
ACTION_WATCH = "watch"
ACTION_AVOID = "avoid"


def _strategy_labels() -> tuple[dict[str, str], dict[str, str]]:
    zh = {item["code"]: item["name"] for item in strategy_library.RULES}
    en = {
        "trend_pullback": "trend pullback",
        "breakout_20": "20-day breakout",
        "mean_reversion": "oversold bounce",
        "pullback_quiet": "quiet pullback",
        "momentum_20": "steady momentum",
        "low_vol_trend": "low-volatility trend",
        "washout_bounce": "washout stabilisation",
    }
    return zh, en


def _headline(action: str, plan, ind: dict, name: str, locale: str = "zh-CN") -> tuple[str, str]:
    """一句话买卖结论（中英双语），确保切换语言不出现混杂。"""
    if plan is None:
        return (
            "现在不需要动手，等条件变好。",
            "Nothing to do right now; wait for the setup to improve.",
        )
    return (
        f"挂限价单在 {plan.entry_low:.2f}–{plan.entry_high:.2f} 之间买入，"
        f"跌破 {plan.stop:.2f} 卖出认错，先看 {plan.target1:.2f}。",
        f"Limit buy between {plan.entry_low:.2f} and {plan.entry_high:.2f}; "
        f"exit below {plan.stop:.2f}; first target {plan.target1:.2f}.",
    )


HOLD_EN = {
    "fast": "usually held for 2 to 5 trading days",
    "short": "usually held for days to a few weeks",
    "medium": "usually held for weeks to a couple of months",
    "long": "usually held for months, managed on monthly bars",
}

HOLD_ZH = {
    "fast": "通常持有 2 到 5 个交易日（快波段）",
    "short": "通常持有几天到几周",
    "medium": "通常持有几周到两个月",
    "long": "通常持有几个月，按月线级别管理",
}

_ROMAN = ("Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ")


def _sector_key(sector: str) -> str:
    text = (sector or "").strip()
    while text and text[-1] in _ROMAN:
        text = text[:-1]
    return text


def _learn(plan, ind: dict, signal, risk_share: float, horizon: str = "short") -> tuple[str, str]:
    """学习模式的大白话解释（中英双语）。"""
    if plan is None:
        return (
            "这是什么：现在不需要动手。\n"
            "为什么不做：价格形态或风险回报不满足进场条件，硬做等于赌。\n"
            "如果硬做会怎样：止损位不好找，亏的钱会比赚的多。",
            "What this is: nothing to do right now.\n"
            "Why not: the setup or the reward does not clear the entry rules; forcing it is gambling.\n"
            "If forced: the stop is hard to place, so the loss ends up bigger than the win.",
        )
    zh_names, en_names = _strategy_labels()
    risk_pct = float((plan.entry_low - plan.stop) / plan.entry_low * 100)
    name = zh_names.get(signal.code, signal.code)
    name_en = en_names.get(signal.code, signal.code)
    last = float(ind.get("last") or 0)
    zh = (
        f"这是什么：一笔做多（先买后卖）的计划，{HOLD_ZH.get(horizon, HOLD_ZH['short'])}。\n"
        f"为什么现在做：「{name}」在 {last:.2f} 附近给出信号。\n"
        f"如果错了会怎样：跌破 {plan.stop:.2f}（约 −{risk_pct:.1f}%）就卖，"
        f"这一笔最多亏 {plan.risk_amount:.2f}，占你总资产约 {risk_share:.1f}%。"
    )
    en = (
        f"What this is: a long plan, {HOLD_EN.get(horizon, HOLD_EN['short'])}.\n"
        f"Why now: “{name_en}” fired near {last:.2f}.\n"
        f"If wrong: sell below {plan.stop:.2f} (about −{risk_pct:.1f}%). "
        f"The most this trade loses is {plan.risk_amount:.2f}, about {risk_share:.1f}% of equity."
    )
    return zh, en


def _used_exposure_cny() -> Decimal:
    """当前已有持仓占用成本（CNY）。"""
    total = Decimal("0")
    try:
        for position in journal_service.positions():
            total += to_cny(
                Decimal(str(position["entry"])) * Decimal(str(position["qty"])),
                position["currency"],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read open positions: %s", exc)
    return total


def _plan_cost_cny(plan) -> Decimal:
    return to_cny(Decimal(str(plan.entry_low)) * plan.qty, plan.currency)


def _exceeds_exposure(cost_cny: Decimal, cap: Decimal, used: Decimal, added: Decimal) -> bool:
    if cap <= 0:
        return False
    return used + added + cost_cny > cap


def _resolve_horizon(settings_map: dict) -> tuple[str, dict | None]:
    from kestrel.services import advisor

    return advisor.resolve(settings_map.get("trade.horizon", timeframe.AUTO))


async def _load_bars(conn, symbols: list[dict], limit: int = 260) -> dict[int, list[dict]]:
    """读取本地 K 线缓存，并发补齐过期或深度不足的历史 K 线。"""
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
    clean = [str(m).upper() for m in markets if str(m).strip()]
    if not clean:
        return []
    conn = connect()
    try:
        marks = ",".join("?" * len(clean))
        # 修复漏洞：自选股也必须查出 s.sector，否则自选股完全绕过行业集中度上限
        watch = conn.execute(
            f"""
            SELECT s.id, s.market, s.code, s.name, s.currency, s.lot_size, s.secid, s.sector
            FROM watchlist w JOIN symbol s ON s.id = w.symbol_id
            WHERE s.market IN ({marks})
            """,
            tuple(clean),
        ).fetchall()
        rows = [dict(r) for r in watch]
        if mode in ("pool", "both"):
            seen = {(r["market"], r["code"]) for r in rows}
            for item in universe_service.pool_symbols(clean, pool_size):
                if (item["market"], item["code"]) in seen:
                    continue
                rows.append(item)
        return rows
    finally:
        conn.close()


async def run_scan(trigger: str = "manual", markets: list[str] | None = None) -> dict:
    from kestrel.services import advisor

    started = time.perf_counter()
    conn = connect()
    scan_id = 0
    try:
        settings_map = config_service.all_values(conn)
        snap = portfolio_service.snapshot(conn=conn, include_quotes=False)
        markets = [
            str(m).upper()
            for m in (markets or settings_map.get("scan.markets") or ["CN", "HK", "US"])
            if str(m).strip()
        ] or ["CN", "HK", "US"]
        min_score = float(settings_map.get("scan.min_score", 0.55))
        capital = float(snap["total_equity_cny"] or settings_map.get("account.capital") or 50000)
        available_cash = float(snap["available_cash_cny"])
        risk_pct = float(settings_map.get("risk.per_trade_pct", 2.0))
        mode = str(settings_map.get("scan.universe", "both"))
        pool_size = int(settings_map.get("scan.pool_size", 30))
        exposure_pct = float(settings_map.get("risk.max_total_exposure_pct", 60) or 60)
        requested_horizon = str(settings_map.get("trade.horizon", timeframe.AUTO))
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

        # 如果本次扫描刚把冷启动缓存填满，重新评估一次周期与市场环境状态
        if advice is None or advice.get("cold_start"):
            try:
                advice = advisor.evaluate(force=True)
                if requested_horizon == timeframe.AUTO and timeframe.is_valid(advice.get("horizon")):
                    horizon = timeframe.get(advice["horizon"])
            except Exception:  # noqa: BLE001
                pass

        market_regimes = ((advice or {}).get("metrics") or {}).get("market_regimes") or {}

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
        blocked = rule_service.blocked_for(horizon.code)
        allowed = {code: code not in blocked for code in strategies}

        signals = plans = 0
        near: list[dict] = []
        rejected: list[dict] = []
        blocked_lot = 0
        sector_count: dict[str, int] = {}
        max_per_sector = int(settings_map.get("risk.max_per_sector", 2))

        exposure_cap = Decimal(str(capital)) * Decimal(str(exposure_pct)) / Decimal("100")
        exposure_used = _used_exposure_cny()
        exposure_added = Decimal("0")
        cash_remaining = available_cash

        for sym in symbols:
            rows = prepared.get(sym["id"]) or []
            if not rows:
                continue
            ind, prev_ind = compute_pair(Series.from_bars(rows))
            if not ind:
                continue

            # 大盘环境滤网：弱势下跌市场提高触发门槛，宁缺毋滥
            mkt_state = (market_regimes.get(sym["market"]) or {}).get("state")
            effective_min = min(0.9, min_score + 0.10) if mkt_state == "bear" else min_score

            best = None
            best_any = None
            for strategy in strategies.values():
                if not allowed.get(strategy.code, True):
                    continue
                result = strategy.evaluate(ind, prev_ind)
                if result is None:
                    continue
                if best_any is None or result.score > best_any.score:
                    best_any = result
                if result.score >= effective_min and (best is None or result.score > best.score):
                    best = result

            if best is None:
                if best_any is not None and best_any.score >= effective_min * 0.75:
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
                available_cash_cny=cash_remaining,
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
            cash_remaining = max(0.0, cash_remaining - float(cost_cny))

            plans += 1

            risk_cny = to_cny(plan.risk_amount, plan.currency)
            risk_share = float(risk_cny) / capital * 100 if capital else 0.0
            zh_headline, _en_headline = _headline(ACTION_BUY, plan, ind, sym["name"])
            zh_learn, en_learn = _learn(plan, ind, best, risk_share, horizon.code)

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
                    zh_headline,
                    zh_learn,
                    en_learn,
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
                "requested": requested_horizon,
                "auto": advice is not None,
                "reason": (advice or {}).get("primary_reason") or "",
            },
            "exposure": {
                "cap": float(exposure_cap),
                "used": float(exposure_used),
                "added": float(exposure_added),
                "pct": exposure_pct,
                "cash_left": round(cash_remaining, 2),
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


def active_recommendations(limit: int = 50, markets: list[str] | None = None) -> list[dict]:
    conn = connect()
    try:
        where_mkt = ""
        params: list = []
        if markets:
            clean = [str(m).upper() for m in markets if str(m).strip()]
            if clean:
                marks = ",".join("?" * len(clean))
                where_mkt = f" AND r.market IN ({marks})"
                params.extend(clean)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT r.*, s.sector AS sector
            FROM recommendation r LEFT JOIN symbol s ON s.id = r.symbol_id
            WHERE (r.status='active'
               OR (r.status IN ('executed','skipped') AND r.decided_at >= date('now','-2 day')))
               {where_mkt}
            ORDER BY CASE r.status WHEN 'active' THEN 0 ELSE 1 END,
                     r.created_at DESC, r.score DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def history(limit: int = 40, markets: list[str] | None = None) -> list[dict]:
    conn = connect()
    try:
        where_mkt = ""
        params: list = []
        if markets:
            clean = [str(m).upper() for m in markets if str(m).strip()]
            if clean:
                marks = ",".join("?" * len(clean))
                where_mkt = f" AND r.market IN ({marks})"
                params.extend(clean)
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT r.*, s.sector AS sector
            FROM recommendation r LEFT JOIN symbol s ON s.id = r.symbol_id
            WHERE r.status IN ('executed','skipped','expired','superseded')
            {where_mkt}
            ORDER BY r.created_at DESC LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def decide(rec_id: int, decision: str, note: str = "", fill_price: float | None = None) -> bool:
    """记录用户对建议的操作决定。"""
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
