"""规则存储、四关统计证伪体系与实盘/纸面表现偏离监控。

一条被停用的规则不会被物理删除：市场风格会轮动，在震荡市失效的趋势规则值得在下一轮
趋势行情重新检验。本模块完整串联四道证伪关卡：
1. 第 1 关（cost_sample）：防前视次日开盘 + 扣费滑点样本筛选；
2. 第 2 关（walk_forward）：多折滚动前推 vs 同期同跨度持有基准；
3. 第 3 关（permutation）：固定随机种子置换检验 p 值（若滚动前推正收益但 p >= 0.10，
   强制降级为「不稳定」，杜绝把随机运气误判为「有优势」）；
4. 第 4 关（live_drift）：对比历史推荐单与用户已平仓交易的实际平均 R 与回测期望，
   当样本 >= 3 且显著低于回测预期时发出「表现偏离预警（建议复查）」。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from kestrel.db.engine import connect
from kestrel.strategies import library
from kestrel.strategies.spec import Rule, RuleError, compile_rule, describe

log = logging.getLogger(__name__)

WARMUP = 80
MIN_TRADES = 12

THIN_VERDICT = "样本不足"
THIN_SAMPLE = "样本不足，未覆盖已保存的判定（换更大的候选池再验证）"

GATE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "gate": 1,
        "code": "cost_sample",
        "name_zh": "扣费滑点筛选",
        "name_en": "Fee & Slippage Filter",
    },
    {
        "gate": 2,
        "code": "walk_forward",
        "name_zh": "多折滚动前推",
        "name_en": "Walk-Forward vs Hold",
    },
    {
        "gate": 3,
        "code": "permutation",
        "name_zh": "置换显著性检验",
        "name_en": "Permutation p-value",
    },
    {
        "gate": 4,
        "code": "live_drift",
        "name_zh": "实盘偏离监控",
        "name_en": "Live/Paper Drift",
    },
]


def ensure_seeded() -> int:
    conn = connect()
    added = 0
    try:
        for item in library.RULES:
            row = conn.execute(
                "SELECT builtin, spec FROM strategy_rule WHERE code=?", (item["code"],)
            ).fetchone()
            new_spec = json.dumps(item["spec"], ensure_ascii=False)
            if row:
                # 仅同步内置规则的最新迭代，不覆盖用户自己改过的或自建规则
                if row["builtin"] and row["spec"] != new_spec:
                    conn.execute(
                        "UPDATE strategy_rule SET name=?, note=?, spec=? WHERE code=? AND builtin=1",
                        (item["name"], item.get("note", ""), new_spec, item["code"]),
                    )
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
                    new_spec,
                ),
            )
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def build_gates_view(validation: dict | None, drift_item: dict | None = None) -> list[dict[str, Any]]:
    """根据已存验证记录与最新实盘偏离状态合成统一的四关证伪视图。"""
    drift = drift_item or {
        "status": "insufficient",
        "warning": False,
        "sample_size": 0,
        "actual_avg_r": None,
        "verdict_zh": "样本积累中 (0/3)",
        "verdict_en": "Collecting samples (0/3)",
    }

    gate4_status = (
        "warn"
        if drift.get("warning")
        else ("passed" if drift.get("status") == "aligned" else "pending")
    )
    gate4_val = (
        f"{drift['actual_avg_r']:+.2f}R (n={drift['sample_size']})"
        if drift.get("actual_avg_r") is not None and drift.get("sample_size", 0) >= 3
        else f"n={drift.get('sample_size', 0)}/3"
    )
    gate4_obj = {
        "gate": 4,
        "code": "live_drift",
        "name_zh": "实盘偏离监控",
        "name_en": "Live/Paper Drift",
        "status": gate4_status,
        "passed": gate4_status == "passed",
        "value": gate4_val,
        "detail_zh": drift.get("verdict_zh", ""),
        "detail_en": drift.get("verdict_en", ""),
    }

    if validation and isinstance(validation.get("gates"), list) and len(validation["gates"]) >= 3:
        existing = [dict(g) for g in validation["gates"][:3]]
        existing.append(gate4_obj)
        return existing

    if not validation or not validation.get("folds"):
        return [
            {
                "gate": g["gate"],
                "code": g["code"],
                "name_zh": g["name_zh"],
                "name_en": g["name_en"],
                "status": "pending",
                "passed": False,
                "value": "—",
                "detail_zh": "尚未运行历史证伪检验",
                "detail_en": "Not validated yet",
            }
            for g in GATE_DEFINITIONS[:3]
        ] + [gate4_obj]

    # 兼容旧版只存了 folds 列表的历史记录，动态推算前 3 关状态
    folds = validation.get("folds") or []
    traded = [r for r in folds if int(r.get("n") or 0) >= MIN_TRADES]
    total_n = sum(int(r.get("n") or 0) for r in folds)
    avg_exp = float(validation.get("avg_exp") or 0.0)
    avg_edge = float(validation.get("avg_edge") or 0.0)
    beating = sum(1 for r in traded if float(r.get("edge") or 0.0) > 0)
    positive = sum(1 for r in traded if float(r.get("exp") or 0.0) > 0)
    p_val = validation.get("p_value")

    g1_ok = bool(traded and avg_exp > 0)
    g2_ok = bool(traded and positive == len(traded) and beating == len(traded) and avg_edge > 0.05)
    g3_status = (
        "pending"
        if p_val is None
        else ("passed" if p_val < 0.05 else ("warn" if p_val < 0.10 else "failed"))
    )

    return [
        {
            "gate": 1,
            "code": "cost_sample",
            "name_zh": "扣费滑点筛选",
            "name_en": "Fee & Slippage Filter",
            "status": "passed" if g1_ok else ("pending" if not traded else "failed"),
            "passed": g1_ok,
            "value": f"{total_n} 笔 · {avg_exp:+.2f}R",
            "detail_zh": "次日开盘成交扣除手续费与滑点后净期望 > 0" if g1_ok else "扣费滑点后样本或净期望未达标",
            "detail_en": "Positive net expectancy after fees & slippage" if g1_ok else "Failed net expectancy or thin sample",
        },
        {
            "gate": 2,
            "code": "walk_forward",
            "name_zh": "多折滚动前推",
            "name_en": "Walk-Forward vs Hold",
            "status": "passed" if g2_ok else ("warn" if (traded and avg_edge > 0) else ("pending" if not traded else "failed")),
            "passed": g2_ok,
            "value": f"{beating}/{len(traded)} 折 · {avg_edge:+.2f}R" if traded else "—",
            "detail_zh": "全部有效折正期望且显著跑赢同期持有基准" if g2_ok else "未能稳定跑赢同期买入持有基准",
            "detail_en": "Beats buy-and-hold across all folds" if g2_ok else "Inconsistent edge over buy-and-hold",
        },
        {
            "gate": 3,
            "code": "permutation",
            "name_zh": "置换显著性检验",
            "name_en": "Permutation p-value",
            "status": g3_status,
            "passed": g3_status == "passed",
            "value": f"p = {p_val:.3f}" if p_val is not None else "—",
            "detail_zh": "重跑验证以生成固定种子置换 p 值" if p_val is None else f"500 次随机打乱经验 p = {p_val:.3f}",
            "detail_en": "Re-validate to compute permutation p-value" if p_val is None else f"500-shuffle empirical p = {p_val:.3f}",
        },
        gate4_obj,
    ]


def all_rules() -> list[dict]:
    ensure_seeded()
    drift_map = live_drift_report()
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
            val = _load_validation(item["code"])
            drift_item = drift_map.get(item["code"])
            item["validation"] = val
            item["drift"] = drift_item
            item["gates"] = build_gates_view(val, drift_item)
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
        parsed = json.loads(item["folds"] or "[]")
    except (TypeError, ValueError):
        parsed = []

    if isinstance(parsed, dict):
        item["folds"] = parsed.get("folds") or []
        item["p_value"] = parsed.get("p_value")
        item["permutation"] = parsed.get("permutation")
        item["gates"] = parsed.get("gates") or []
        item["downgraded_by_perm"] = bool(parsed.get("downgraded_by_perm", False))
    else:
        item["folds"] = parsed if isinstance(parsed, list) else []
        item["p_value"] = None
        item["permutation"] = None
        item["gates"] = []
        item["downgraded_by_perm"] = False
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
            (
                rule.code,
                rule.name,
                str(payload.get("note") or ""),
                json.dumps(spec, ensure_ascii=False),
            ),
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


# ------------------------------------------------------------------ validation & 4-gate falsification


def _stats(trades: list[dict]) -> dict:
    """Per-fold summary, including the numbers that decide whether it survives."""
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
    """计算同期同跨度被动买入持有基准的 R 倍数收益（与次日开盘成交窗口严格对齐）。"""
    i = int(trade["index"])
    if i < 0 or i >= len(chunk) or not trade.get("risk"):
        return None
    held = max(1, int(trade.get("bars") or 1))
    j = min(len(chunk) - 1, i + held - 1)
    entry = float(chunk[i].get("open", chunk[i]["close"]))
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


def _should_record(verdict: str, persist: bool) -> bool:
    """样本不足代表尚无定论，绝不覆盖用户此前已测得的真实结论。"""
    return bool(persist) and verdict != THIN_VERDICT


def live_drift_report(
    code: str | None = None,
    *,
    backtest_exp_override: float | None = None,
    min_samples: int = 3,
) -> dict[str, dict[str, Any]]:
    """第 4 关证伪：对比各策略历史推荐单及已平仓实盘交易的实际平均 R 与回测 avg_exp。

    当某策略有效样本数 >= 3 且实际平均表现显著低于回测预期时，标记 ``warning=True``
    并给出「表现偏离预警（建议复查）」。
    """
    ensure_seeded()
    conn = connect()
    try:
        if code:
            rule_rows = conn.execute(
                "SELECT code, name FROM strategy_rule WHERE code=?", (code,)
            ).fetchall()
        else:
            rule_rows = conn.execute(
                "SELECT code, name FROM strategy_rule ORDER BY builtin DESC, code"
            ).fetchall()

        # 读取各策略最近一次验证的回测 avg_exp
        bt_map: dict[str, float] = {}
        for rv in conn.execute(
            """
            SELECT rv.code, rv.avg_exp
            FROM rule_validation rv
            JOIN (
                SELECT code, MAX(id) AS max_id FROM rule_validation GROUP BY code
            ) latest ON latest.max_id = rv.id
            """
        ).fetchall():
            bt_map[rv["code"]] = float(rv["avg_exp"] or 0.0)

        if code and backtest_exp_override is not None:
            bt_map[code] = float(backtest_exp_override)

        # 1) 统计用户已平仓交易的实际实现 R（兼容直接记录 strategy 或通过 rec_id 关联推荐单）
        closed_rows = conn.execute(
            """
            SELECT t.id, t.rec_id, t.qty, t.entry_price, t.exit_price, t.fees,
                   t.initial_stop, t.stop_price,
                   COALESCE(NULLIF(t.strategy, ''), r.strategy) AS strat_code,
                   r.stop AS rec_stop
            FROM trade t
            LEFT JOIN recommendation r ON r.id = t.rec_id
            WHERE t.status = 'closed' AND t.exit_price IS NOT NULL
            """
        ).fetchall()

        trade_rs_by_code: dict[str, list[float]] = {}
        covered_rec_ids: set[int] = set()
        for tr in closed_rows:
            strat = tr["strat_code"]
            if not strat:
                continue
            if tr["rec_id"]:
                covered_rec_ids.add(int(tr["rec_id"]))
            entry_p = float(tr["entry_price"] or 0) / 1_000_000.0
            exit_p = float(tr["exit_price"] or 0) / 1_000_000.0
            qty_f = max(float(tr["qty"] or 0), 1e-9)
            if entry_p <= 0:
                continue
            fees_ps = (float(tr["fees"] or 0) / 1_000_000.0) / qty_f
            stop_micro = tr["initial_stop"] or tr["stop_price"] or tr["rec_stop"]
            stop_p = (float(stop_micro) / 1_000_000.0) if stop_micro else 0.0
            risk_ps = (entry_p - stop_p) if (stop_p > 0 and entry_p > stop_p) else (entry_p * 0.03)
            actual_r = ((exit_p - entry_p) - fees_ps) / max(risk_ps, 1e-6)
            trade_rs_by_code.setdefault(strat, []).append(actual_r)

        # 2) 统计历史推荐单总数及未关联已平仓 trade 但已有后续市场报价的纸面跟踪 R
        rec_rows = conn.execute(
            """
            SELECT r.id, r.strategy, r.entry_low, r.stop, r.qty, r.est_fees,
                   r.fill_price, r.status, q.price AS latest_price
            FROM recommendation r
            LEFT JOIN quote_latest q ON q.symbol_id = r.symbol_id
            WHERE r.strategy IS NOT NULL AND r.strategy != ''
            """
        ).fetchall()

        rec_total_by_code: dict[str, int] = {}
        paper_rs_by_code: dict[str, list[float]] = {}
        for rec in rec_rows:
            strat = rec["strategy"]
            rec_total_by_code[strat] = rec_total_by_code.get(strat, 0) + 1
            if int(rec["id"]) in covered_rec_ids:
                continue
            # 仅对已到期/已执行且具备最新报价的推荐单计算纸面完结表现
            if rec["status"] in ("expired", "executed") and rec["latest_price"]:
                entry_p = (
                    float(rec["fill_price"]) / 1_000_000.0
                    if rec["fill_price"]
                    else float(rec["entry_low"] or 0) / 1_000_000.0
                )
                stop_p = float(rec["stop"] or 0) / 1_000_000.0
                mark_p = float(rec["latest_price"]) / 1_000_000.0
                qty_f = max(float(rec["qty"] or 1), 1.0)
                fee_ps = (float(rec["est_fees"] or 0) / 1_000_000.0) / qty_f
                if entry_p > 0 and entry_p > stop_p > 0 and mark_p > 0:
                    risk_ps = entry_p - stop_p
                    raw_r = ((mark_p - entry_p) - fee_ps) / max(risk_ps, 1e-6)
                    clipped_r = max(-2.5, min(4.0, raw_r))
                    paper_rs_by_code.setdefault(strat, []).append(clipped_r)
    finally:
        conn.close()

    report: dict[str, dict[str, Any]] = {}
    for row in rule_rows:
        rcode = row["code"]
        t_rs = trade_rs_by_code.get(rcode, [])
        p_rs = paper_rs_by_code.get(rcode, [])
        combined = t_rs + p_rs
        n_samples = len(combined)
        bt_exp = bt_map.get(rcode)
        actual_avg = round(sum(combined) / n_samples, 4) if n_samples > 0 else None
        trade_avg = round(sum(t_rs) / len(t_rs), 4) if t_rs else None

        if n_samples < min_samples:
            drift_val = (
                round(actual_avg - bt_exp, 4)
                if (actual_avg is not None and bt_exp is not None)
                else None
            )
            report[rcode] = {
                "code": rcode,
                "status": "insufficient",
                "warning": False,
                "sample_size": n_samples,
                "closed_trades": len(t_rs),
                "paper_samples": len(p_rs),
                "recommendations": rec_total_by_code.get(rcode, 0),
                "actual_avg_r": actual_avg,
                "trade_avg_r": trade_avg,
                "backtest_exp": round(bt_exp, 4) if bt_exp is not None else None,
                "drift_r": drift_val,
                "verdict_zh": f"样本积累中 ({n_samples}/{min_samples})",
                "verdict_en": f"Collecting live samples ({n_samples}/{min_samples})",
            }
            continue

        assert actual_avg is not None
        ref_exp = bt_exp if bt_exp is not None else 0.10
        drift_val = round(actual_avg - ref_exp, 4)
        # 当实际表现较回测期望显著恶化（落后超过 0.35R，或回测为正而实盘明显亏损且落后 >= 0.25R）时告警
        is_warn = bool(
            drift_val <= -0.35
            or (ref_exp > 0 and actual_avg < -0.10 and drift_val <= -0.25)
        )
        if is_warn:
            v_zh = "表现偏离预警（建议复查）"
            v_en = "Performance drift alert (review advised)"
            status_code = "warning"
        else:
            v_zh = "实盘与回测预期基本一致"
            v_en = "Aligned with backtest expectation"
            status_code = "aligned"

        report[rcode] = {
            "code": rcode,
            "status": status_code,
            "warning": is_warn,
            "sample_size": n_samples,
            "closed_trades": len(t_rs),
            "paper_samples": len(p_rs),
            "recommendations": rec_total_by_code.get(rcode, 0),
            "actual_avg_r": actual_avg,
            "trade_avg_r": trade_avg,
            "backtest_exp": round(bt_exp, 4) if bt_exp is not None else None,
            "drift_r": drift_val,
            "verdict_zh": v_zh,
            "verdict_en": v_en,
        }

    return report


def validate(
    code: str,
    folds: int = 3,
    max_symbols: int = 200,
    horizon: str = "short",
    persist: bool = True,
    fee_drag_pct: float = 0.002,
    slippage_bps: float = 10.0,
) -> dict:
    """串联四道量化证伪关卡执行多折滚动前推检验，并持久化判定结果。"""
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
    all_oos_trades: list[dict] = []
    all_oos_bench: list[float] = []

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
                    min_bars=cfg.warmup,
                    stop_band=cfg.stop_band,
                    fee_drag_pct=fee_drag_pct,
                    next_open=True,
                    slippage_bps=slippage_bps,
                    registry_override=rules,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("validate %s failed: %s", code, exc)
                continue
            for t in result["trades"]:
                if t["index"] < offset:
                    continue
                hold = _hold_r(chunk, t)
                bench_val = hold if hold is not None else 0.0
                trades.append(t)
                if hold is not None:
                    bench.append(hold)
                all_oos_trades.append(t)
                all_oos_bench.append(bench_val)
        st = _stats(trades)
        st["bench"] = (sum(bench) / len(bench)) if bench else 0.0
        st["edge"] = st["exp"] - st["bench"]
        st["fold"] = f + 1
        fold_rows.append(st)

    # 第 3 关：对全部样本外交易相对同期基准做固定随机种子（seed=42）置换显著性检验
    perm = backtest.permutation_test(
        all_oos_trades,
        benchmark_rs=all_oos_bench,
        n_perms=500,
        seed=42,
    )
    p_value = float(perm["p_value"])

    downgraded_by_perm = False
    traded = [r for r in fold_rows if r["n"] >= MIN_TRADES]
    if not traded:
        verdict = THIN_VERDICT
        avg_exp = avg_edge = 0.0
        positive = beating = 0
    else:
        avg_exp = sum(r["exp"] for r in traded) / len(traded)
        avg_edge = sum(r["edge"] for r in traded) / len(traded)
        positive = sum(1 for r in traded if r["exp"] > 0)
        beating = sum(1 for r in traded if r["edge"] > 0)
        if positive == len(traded) and beating == len(traded) and avg_edge > 0.05:
            if p_value >= 0.10:
                # 关键证伪守卫：滚动前推表面为正但置换 p >= 0.10 无法排除运气，降级为「不稳定」
                verdict = "不稳定"
                downgraded_by_perm = True
            else:
                verdict = "有优势"
        elif avg_edge <= 0:
            verdict = "无优势"
        else:
            verdict = "不稳定"

    total_trades = sum(row["n"] for row in fold_rows)
    drift_item = live_drift_report(code=code, backtest_exp_override=avg_exp).get(code, {})

    g1_ok = bool(traded and avg_exp > 0)
    g2_ok = bool(traded and positive == len(traded) and beating == len(traded) and avg_edge > 0.05)
    g3_status = (
        "pending"
        if not traded
        else ("passed" if p_value < 0.05 else ("warn" if p_value < 0.10 else "failed"))
    )
    g4_status = (
        "warn"
        if drift_item.get("warning")
        else ("passed" if drift_item.get("status") == "aligned" else "pending")
    )

    gates = [
        {
            "gate": 1,
            "code": "cost_sample",
            "name_zh": "扣费滑点筛选",
            "name_en": "Fee & Slippage Filter",
            "status": "passed" if g1_ok else ("pending" if not traded else "failed"),
            "passed": g1_ok,
            "value": f"{total_trades} 笔 · {avg_exp:+.2f}R",
            "detail_zh": (
                f"次日开盘成交扣费滑点后样本 {total_trades} 笔，净期望 {avg_exp:+.2f}R"
                if traded
                else f"单折有效样本不足 {MIN_TRADES} 笔"
            ),
            "detail_en": (
                f"{total_trades} trades after fees & slippage, net {avg_exp:+.2f}R"
                if traded
                else f"Fewer than {MIN_TRADES} trades per fold"
            ),
        },
        {
            "gate": 2,
            "code": "walk_forward",
            "name_zh": "多折滚动前推",
            "name_en": "Walk-Forward vs Hold",
            "status": "passed" if g2_ok else ("warn" if (traded and avg_edge > 0) else ("pending" if not traded else "failed")),
            "passed": g2_ok,
            "value": f"{beating}/{len(traded)} 折 · {avg_edge:+.2f}R" if traded else "—",
            "detail_zh": (
                f"{beating}/{len(traded)} 折跑赢同期买入持有，平均超额 {avg_edge:+.2f}R"
                if traded
                else "等待更多历史数据折"
            ),
            "detail_en": (
                f"Beat buy-and-hold in {beating}/{len(traded)} folds ({avg_edge:+.2f}R)"
                if traded
                else "Awaiting deeper bar history"
            ),
        },
        {
            "gate": 3,
            "code": "permutation",
            "name_zh": "置换显著性检验",
            "name_en": "Permutation p-value",
            "status": g3_status,
            "passed": g3_status == "passed",
            "value": f"p = {p_value:.3f}" if traded else "—",
            "detail_zh": perm["verdict_zh"],
            "detail_en": perm["verdict_en"],
        },
        {
            "gate": 4,
            "code": "live_drift",
            "name_zh": "实盘偏离监控",
            "name_en": "Live/Paper Drift",
            "status": g4_status,
            "passed": g4_status == "passed",
            "value": (
                f"{drift_item['actual_avg_r']:+.2f}R (n={drift_item['sample_size']})"
                if drift_item.get("actual_avg_r") is not None and drift_item.get("sample_size", 0) >= 3
                else f"n={drift_item.get('sample_size', 0)}/3"
            ),
            "detail_zh": drift_item.get("verdict_zh", ""),
            "detail_en": drift_item.get("verdict_en", ""),
        },
    ]

    record = _should_record(verdict, persist)
    disabled = False
    if record:
        folds_payload = {
            "folds": fold_rows,
            "p_value": p_value,
            "permutation": perm,
            "gates": gates,
            "downgraded_by_perm": downgraded_by_perm,
        }
        conn = connect()
        try:
            conn.execute(
                """
                INSERT INTO rule_validation (code, verdict, avg_exp, avg_edge, folds,
                                             symbols, timeframe, created_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))
                """,
                (
                    code,
                    verdict,
                    round(avg_exp, 4),
                    round(avg_edge, 4),
                    json.dumps(folds_payload, ensure_ascii=False),
                    len(universe),
                    cfg.code,
                ),
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

    if not record:
        note = THIN_SAMPLE
    elif downgraded_by_perm:
        note = f"滚动前推为正，但置换检验 p={p_value:.3f} >= 0.10 无法排除随机运气，已自动降级为「不稳定」。"
    elif drift_item.get("warning"):
        note = "注意：近期实盘/推荐样本表现显著低于回测预期，建议复查市场环境是否切换。"
    else:
        note = ""

    return {
        "ok": True,
        "code": code,
        "timeframe": cfg.code,
        "verdict": verdict,
        "avg_exp": round(avg_exp, 4),
        "avg_edge": round(avg_edge, 4),
        "p_value": p_value,
        "permutation": perm,
        "gates": gates,
        "drift": drift_item,
        "downgraded_by_perm": downgraded_by_perm,
        "folds": fold_rows,
        "trades": total_trades,
        "symbols": len(universe),
        "persisted": record,
        "note": note,
        "auto_disabled": disabled,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def blocked_for(timeframe_code: str) -> set[str]:
    """Rules measured as losing money on this bar size."""
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
