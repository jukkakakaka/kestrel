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
    """Per-fold summary, including the numbers that decide whether it survives.

    Drawdown and loss streaks are computed on trades ordered by entry date: the
    scan walks symbols in pool order, which has nothing to do with the passage
    of time, so the raw list would produce a meaningless "equity curve".
    """
    if not trades:
        return {"n": 0, "win": 0.0, "exp": 0.0, "pf": 0.0, "dd": 0.0, "streak": 0}

    ordered = sorted(
        trades, key=lambda t: (str(t.get("date") or ""), int(t.get("index") or 0))
    )
    wins = [t["r"] for t in ordered if t["r"] > 0]
    losses = [t["r"] for t in ordered if t["r"] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    running = peak = dd = 0.0
    streak = worst = 0
    for trade in ordered:
        running += trade["r"]
        peak = max(peak, running)
        dd = min(dd, running - peak)
        if trade["r"] <= 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0

    return {
        "n": len(ordered),
        "win": len(wins) / len(ordered) * 100,
        "exp": running / len(ordered),
        "pf": round(gross_win / gross_loss, 2) if gross_loss else (99.0 if gross_win else 0.0),
        "dd": round(dd, 2),
        "streak": worst,
    }


def _hold_r(chunk: list[dict], trade: dict) -> float | None:
    i = trade["index"]
    held = max(1, int(trade.get("bars") or 1))
    j = min(len(chunk) - 1, i + held)
    if j <= i or not trade.get("risk"):
        return None
    entry = float(chunk[i]["close"])
    return (float(chunk[j]["close"]) - entry) / float(trade["risk"])


def _windows(length: int, folds: int, warmup: int = WARMUP, min_span: int = 40) -> list[tuple[int, int]]:
    if length < warmup * 2:
        return []
    start = warmup + int((length - warmup) * 0.35)
    step = max(min_span, (length - start) // folds)
    out = []
    for i in range(folds):
        lo = start + i * step
        hi = min(length, lo + step)
        if hi - lo < min_span:
            break
        out.append((lo, hi))
    return out


THIN_VERDICT = "样本不足"
THIN_SAMPLE = "样本不足，未覆盖已保存的判定（换更大的候选池再验证）"


def _should_record(verdict: str, persist: bool) -> bool:
    """"Insufficient sample" is the absence of a verdict, not a verdict.

    Storing it would overwrite a measurement the user already paid for with
    "we could not tell" — the one direction of change that loses information.
    """
    return bool(persist) and verdict != THIN_VERDICT


def validate(
    code: str,
    folds: int = 3,
    max_symbols: int = 200,
    horizon: str = "short",
    persist: bool = True,
) -> dict:
    """Walk-forward test with a fair benchmark, then store the verdict.

    Verdicts are per bar size: a rule that loses money on daily bars can still be
    the only thing that works on weekly ones, and one number for both would hide
    that. The global ``enabled`` flag is only cleared when every measured bar size
    says the same thing.
    """
    from kestrel.services import backtest, timeframe
    from kestrel.services import bars as bars_service

    cfg = timeframe.get(horizon)
    rules = registry(enabled_only=False)
    rule = rules.get(code)
    if rule is None:
        raise RuleError(f"找不到规则 {code}")

    conn = connect()
    rows = conn.execute(
        """
        SELECT id, market FROM symbol
        WHERE is_pool=1 OR id IN (SELECT symbol_id FROM watchlist)
        ORDER BY is_pool DESC, pool_rank LIMIT ?
        """,
        (max_symbols,),
    ).fetchall()
    universe = []
    for row in rows:
        chunk = bars_service.drop_forming(
            bars_service.cached_bars(conn, row["id"], cfg.lookback), row["market"]
        )
        folded = timeframe.prepare(chunk, cfg.code)
        if len(folded) >= cfg.warmup * 2 + 20:
            universe.append(folded)
    conn.close()

    if not universe:
        return {
            "ok": False,
            "error": "没有足够的历史数据：先在对应周期上跑一次扫描，把 K 线缓存深一点。",
            "timeframe": cfg.code,
        }

    fold_rows = []
    for f in range(folds):
        trades: list[dict] = []
        bench: list[float] = []
        for bars in universe:
            windows = _windows(len(bars), folds, cfg.warmup, cfg.min_span)
            if f >= len(windows):
                continue
            lo, hi = windows[f]
            slice_start = max(0, lo - cfg.warmup)
            chunk = bars[slice_start:hi]
            offset = lo - slice_start
            try:
                result = backtest.run(
                    chunk,
                    code,
                    max_hold=cfg.max_hold,
                    # A fold carries exactly its warmup plus its span, which can
                    # be shorter than the daily default of 60 bars — that guard
                    # would silently turn every weekly fold into "no trades".
                    min_bars=cfg.warmup,
                    stop_band=cfg.stop_band,
                    registry_override=rules,
                )
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
        verdict = THIN_VERDICT
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

    record = _should_record(verdict, persist)
    disabled = False
    if record:
        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO rule_validation (code, verdict, avg_exp, avg_edge, folds,
                                             symbols, timeframe, created_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))
                """,
                (code, verdict, round(avg_exp, 4), round(avg_edge, 4),
                 json.dumps(fold_rows, ensure_ascii=False), len(universe), cfg.code),
            )
            conn.execute(
                """
                INSERT INTO rule_timeframe_state (code, timeframe, verdict, avg_edge,
                                                  symbols, validated_at)
                VALUES (?,?,?,?,?,datetime('now'))
                ON CONFLICT(code, timeframe) DO UPDATE SET
                    verdict=excluded.verdict, avg_edge=excluded.avg_edge,
                    symbols=excluded.symbols, validated_at=excluded.validated_at
                """,
                (code, cfg.code, verdict, round(avg_edge, 4), len(universe)),
            )
            # Switch the rule off only when every bar size we have measured says
            # the same thing. A rule that fails on daily but works on weekly is
            # still worth publishing — on weekly.
            states = [
                r["verdict"]
                for r in conn.execute(
                    "SELECT verdict FROM rule_timeframe_state WHERE code=?", (code,)
                ).fetchall()
            ]
            disabled = bool(states) and all(v == "无优势" for v in states)
            conn.execute(
                "UPDATE strategy_rule SET last_verdict=?, last_validated=datetime('now'),"
                " enabled=CASE WHEN ? THEN 0 ELSE enabled END WHERE code=?",
                (verdict, 1 if disabled else 0, code),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "code": code,
        "timeframe": cfg.code,
        "verdict": verdict,
        "avg_exp": round(avg_exp, 4),
        "avg_edge": round(avg_edge, 4),
        "folds": fold_rows,
        # Per-fold counts can look thin on slow bars; the total is what the user
        # needs to judge whether "insufficient" means "rare" or "broken".
        "trades": sum(row["n"] for row in fold_rows),
        "symbols": len(universe),
        # False means "measured, but deliberately not saved" — the UI has to say
        # so instead of showing the old verdict as if it were the new one.
        "persisted": record,
        "note": "" if record else THIN_SAMPLE,
        "auto_disabled": disabled,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def blocked_for(timeframe_code: str) -> set[str]:
    """Rules measured as losing money on this bar size.

    Missing state means "not measured yet" — the scan still uses it, and the
    rules page labels it as unproven. Going dark because nobody has clicked
    validate would be worse than saying so.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT code FROM rule_timeframe_state WHERE timeframe=? AND verdict='无优势'",
            (timeframe_code,),
        ).fetchall()
        return {r["code"] for r in rows}
    finally:
        conn.close()


def timeframe_states() -> dict[str, dict[str, dict]]:
    """``{code: {timeframe: {verdict, avg_edge, validated_at}}}`` for the UI."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT code, timeframe, verdict, avg_edge, symbols, validated_at"
            " FROM rule_timeframe_state"
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        out.setdefault(row["code"], {})[row["timeframe"]] = dict(row)
    return out


def validate_all(
    folds: int = 3, horizon: str = "short", max_symbols: int = 200
) -> list[dict]:
    out = []
    for item in all_rules():
        try:
            out.append(
                validate(
                    item["code"],
                    folds=folds,
                    max_symbols=max_symbols,
                    horizon=horizon,
                )
            )
        except RuleError as exc:
            out.append({"ok": False, "code": item["code"], "error": str(exc)})
    return out
