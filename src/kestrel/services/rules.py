"""Rules stored as data, plus the validation that decides whether they are kept.

A rule that is disabled is not deleted: market regimes change, and a rule that
failed in one period is worth re-testing in another. This module keeps the whole
history of that judgement.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from kestrel.db.engine import connect
from kestrel.strategies import library
from kestrel.strategies.spec import Rule, RuleError, compile_rule, describe

log = logging.getLogger(__name__)

WARMUP = 80
MIN_TRADES = 12


def ensure_seeded() -> int:
    conn = connect()
    added = 0
    try:
        for item in library.RULES:
            exists = conn.execute(
                "SELECT 1 FROM strategy_rule WHERE code=?", (item["code"],)
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO strategy_rule (code, name, note, spec, enabled, builtin, created_at)
                VALUES (?,?,?,?,1,1,datetime('now'))
                """,
                (
                    item["code"],
                    item["name"],
                    item.get("note", ""),
                    json.dumps(item["spec"], ensure_ascii=False),
                ),
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def all_rules() -> list[dict]:
    ensure_seeded()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM strategy_rule ORDER BY builtin DESC, code"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                spec = json.loads(item["spec"])
            except (TypeError, ValueError):
                spec = {}
            item["spec"] = spec
            try:
                item["summary"] = describe(compile_rule(spec))
            except RuleError as exc:
                item["summary"] = f"规则有误：{exc}"
            item["validation"] = _load_validation(item["code"])
            out.append(item)
        return out
    finally:
        conn.close()


def _load_validation(code: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM rule_validation WHERE code=? ORDER BY id DESC LIMIT 1", (code,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    item = dict(row)
    try:
        item["folds"] = json.loads(item["folds"] or "[]")
    except (TypeError, ValueError):
        item["folds"] = []
    return item


def save(payload: dict) -> str:
    spec = payload.get("spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except (TypeError, ValueError) as exc:
            raise RuleError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(spec, dict):
        raise RuleError("缺少 spec")

    rule = compile_rule(spec)  # raises with a readable message
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO strategy_rule (code, name, note, spec, enabled, builtin, created_at)
            VALUES (?,?,?,?,1,0,datetime('now'))
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name, note=excluded.note, spec=excluded.spec
            """,
            (rule.code, rule.name, str(payload.get("note") or ""),
             json.dumps(spec, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return rule.code


def set_enabled(code: str, enabled: bool) -> bool:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE strategy_rule SET enabled=? WHERE code=?", (int(enabled), code)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete(code: str) -> bool:
    conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM strategy_rule WHERE code=? AND builtin=0", (code,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def registry(enabled_only: bool = True) -> dict[str, Rule]:
    """Compiled rules the scanner may use."""
    ensure_seeded()
    conn = connect()
    try:
        sql = "SELECT code, spec FROM strategy_rule"
        if enabled_only:
            sql += " WHERE enabled=1"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    out: dict[str, Rule] = {}
    for row in rows:
        try:
            spec = json.loads(row["spec"])
            out[row["code"]] = compile_rule(spec)
        except (RuleError, TypeError, ValueError) as exc:
            log.warning("rule %s cannot compile: %s", row["code"], exc)
    return out


# ------------------------------------------------------------------ validation

def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "win": 0.0, "exp": 0.0}
    wins = [t for t in trades if t["r"] > 0]
    total = sum(t["r"] for t in trades)
    return {"n": len(trades), "win": len(wins) / len(trades) * 100, "exp": total / len(trades)}


def _hold_r(chunk: list[dict], trade: dict) -> float | None:
    i = trade["index"]
    held = max(1, int(trade.get("bars") or 1))
    j = min(len(chunk) - 1, i + held)
    if j <= i or not trade.get("risk"):
        return None
    entry = float(chunk[i]["close"])
    return (float(chunk[j]["close"]) - entry) / float(trade["risk"])


def _windows(length: int, folds: int) -> list[tuple[int, int]]:
    if length < WARMUP * 2:
        return []
    start = WARMUP + int((length - WARMUP) * 0.35)
    step = max(WARMUP, (length - start) // folds)
    out = []
    for i in range(folds):
        lo = start + i * step
        hi = min(length, lo + step)
        if hi - lo < 40:
            break
        out.append((lo, hi))
    return out


def validate(code: str, folds: int = 3, max_symbols: int = 200) -> dict:
    """Walk-forward test with a fair benchmark, then store the verdict."""
    from kestrel.services import backtest
    from kestrel.services import bars as bars_service

    rules = registry(enabled_only=False)
    rule = rules.get(code)
    if rule is None:
        raise RuleError(f"找不到规则 {code}")

    conn = connect()
    rows = conn.execute(
        """
        SELECT id FROM symbol
        WHERE is_pool=1 OR id IN (SELECT symbol_id FROM watchlist)
        ORDER BY is_pool DESC, pool_rank LIMIT ?
        """,
        (max_symbols,),
    ).fetchall()
    universe = []
    for row in rows:
        chunk = bars_service.cached_bars(conn, row["id"], 900)
        if len(chunk) >= WARMUP * 2 + 80:
            universe.append(chunk)
    conn.close()

    if not universe:
        return {"ok": False, "error": "没有足够的历史数据"}

    fold_rows = []
    for f in range(folds):
        trades: list[dict] = []
        bench: list[float] = []
        for bars in universe:
            windows = _windows(len(bars), folds)
            if f >= len(windows):
                continue
            lo, hi = windows[f]
            slice_start = max(0, lo - WARMUP)
            chunk = bars[slice_start:hi]
            offset = lo - slice_start
            try:
                result = backtest.run(chunk, code, registry_override=rules)
            except Exception as exc:  # noqa: BLE001
                log.warning("validate %s failed: %s", code, exc)
                continue
            for t in result["trades"]:
                if t["index"] < offset:
                    continue
                trades.append(t)
                hold = _hold_r(chunk, t)
                if hold is not None:
                    bench.append(hold)
        st = _stats(trades)
        st["bench"] = (sum(bench) / len(bench)) if bench else 0.0
        st["edge"] = st["exp"] - st["bench"]
        st["fold"] = f + 1
        fold_rows.append(st)

    traded = [r for r in fold_rows if r["n"] >= MIN_TRADES]
    if not traded:
        verdict = "样本不足"
        avg_exp = avg_edge = 0.0
    else:
        avg_exp = sum(r["exp"] for r in traded) / len(traded)
        avg_edge = sum(r["edge"] for r in traded) / len(traded)
        positive = sum(1 for r in traded if r["exp"] > 0)
        beating = sum(1 for r in traded if r["edge"] > 0)
        if positive == len(traded) and beating == len(traded) and avg_edge > 0.05:
            verdict = "有优势"
        elif avg_edge <= 0:
            verdict = "无优势"
        else:
            verdict = "不稳定"

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO rule_validation (code, verdict, avg_exp, avg_edge, folds,
                                         symbols, created_at)
            VALUES (?,?,?,?,?,?,datetime('now'))
            """,
            (code, verdict, round(avg_exp, 4), round(avg_edge, 4),
             json.dumps(fold_rows, ensure_ascii=False), len(universe)),
        )
        # Measured as no edge: switch it off rather than keep publishing advice
        # from it. The user can turn it back on from the rules page.
        conn.execute(
            "UPDATE strategy_rule SET last_verdict=?, last_validated=datetime('now'),"
            " enabled=CASE WHEN ?='无优势' THEN 0 ELSE enabled END WHERE code=?",
            (verdict, verdict, code),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "code": code,
        "verdict": verdict,
        "avg_exp": round(avg_exp, 4),
        "avg_edge": round(avg_edge, 4),
        "folds": fold_rows,
        "symbols": len(universe),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def validate_all(folds: int = 3) -> list[dict]:
    out = []
    for item in all_rules():
        try:
            out.append(validate(item["code"], folds=folds))
        except RuleError as exc:
            out.append({"ok": False, "code": item["code"], "error": str(exc)})
    return out
