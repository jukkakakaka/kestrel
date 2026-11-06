"""Scan orchestration: bars → indicators → strategies → plans → recommendations."""

from __future__ import annotations

import json
import logging

from kestrel.core.clock import iso, utcnow
from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.indicators.engine import Series, compute
from kestrel.planning import builder
from kestrel.receipts import tokens
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.strategies.base import registry

log = logging.getLogger(__name__)


async def run_scan(trigger: str = "manual", markets: list[str] | None = None) -> dict:
    conn = connect()
    try:
        settings_map = config_service.all_values(conn)
        markets = markets or settings_map.get("scan.markets") or ["CN", "HK", "US"]
        min_score = float(settings_map.get("scan.min_score", 0.55))
        capital = float(settings_map.get("account.capital", 10000))
        risk_pct = float(settings_map.get("risk.per_trade_pct", 2.0))

        cur = conn.execute(
            "INSERT INTO scan_run (trigger, markets) VALUES (?, ?)",
            (trigger, json.dumps(markets)),
        )
        scan_id = cur.lastrowid
        conn.commit()

        symbols = conn.execute(
            """
            SELECT s.id, s.market, s.code, s.name, s.currency, s.lot_size
            FROM watchlist w JOIN symbol s ON s.id = w.symbol_id
            WHERE s.market IN ({})
            """.format(",".join("?" * len(markets))),
            tuple(markets),
        ).fetchall()

        strategies = registry()
        scanned = signals = plans = 0
        near: list[dict] = []
        blocked_lot = 0

        for sym in symbols:
            bars = await bars_service.get_bars(
                conn, sym["id"], sym["market"], sym["code"], limit=260
            )
            if len(bars) < 40:
                continue
            scanned += 1
            ind = compute(Series.from_bars(bars))
            if not ind:
                continue

            best = None
            best_any = None
            for strategy in strategies.values():
                result = strategy.evaluate(ind)
                if result is None:
                    continue
                if best_any is None or result.score > best_any.score:
                    best_any = result
                if result.score >= min_score and (best is None or result.score > best.score):
                    best = result
            if best is None:
                if best_any is not None:
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
            plan = builder.build(
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
                blocked_lot += 1
                continue
            plans += 1

            conn.execute(
                "INSERT INTO signal (scan_id, symbol_id, strategy, score, reasons)"
                " VALUES (?,?,?,?,?)",
                (scan_id, sym["id"], best.code, best.score, json.dumps(best.reasons)),
            )
            cur = conn.execute(
                """
                INSERT INTO recommendation
                    (scan_id, symbol_id, market, code, name, currency, strategy, score,
                     entry_low, entry_high, stop, target1, target2, qty, lot_size,
                     risk_amount, est_fees, breakeven, r_multiple, confidence,
                     valid_until, invalidation, explain_zh, explain_en, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active')
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
                ),
            )
            rec_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO receipt_token (code, rec_id, expires_at)"
                " VALUES (?,?,?)",
                (tokens.make_code(rec_id), rec_id, tokens.expiry()),
            )

        conn.execute(
            "UPDATE scan_run SET finished_at=?, scanned=?, signals=?, plans=?, status='ok'"
            " WHERE id=?",
            (iso(utcnow()), scanned, signals, plans, scan_id),
        )
        conn.commit()
        summary = {
            "ok": True,
            "scan_id": scan_id,
            "scanned": scanned,
            "signals": signals,
            "plans": plans,
            "blocked_lot": blocked_lot,
            "near": sorted(near, key=lambda x: -x["score"])[:5],
            "min_score": min_score,
        }
        config_service.put(conn, "scan.last", summary)
        conn.commit()
        return summary
    except Exception as exc:  # noqa: BLE001
        log.exception("scan failed")
        try:
            conn.execute(
                "UPDATE scan_run SET finished_at=?, status='failed', error=? WHERE id=?",
                (iso(utcnow()), str(exc), locals().get("scan_id")),
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
            SELECT * FROM recommendation
            WHERE status='active'
            ORDER BY created_at DESC, score DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def expire_stale() -> int:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE recommendation SET status='expired'"
            " WHERE status='active' AND valid_until < ?",
            (iso(utcnow()),),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
