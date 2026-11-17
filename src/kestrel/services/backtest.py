"""事件驱动量化回测与统计证伪引擎，与实盘规划及退出管理保持逻辑同构。

核心证伪保障：
1. **严格防前视偏差**：指标仅在截止当根的历史前缀窗口计算；默认启用 ``next_open=True``，
   信号在第 ``i`` 根收盘确认后于第 ``i + 1`` 根开盘价叠加滑点成交（若次日开盘已跌破止损位则
   放弃入场；若信号出现在最后一根 K 线则返回 ``pending_signal`` 而不虚构持仓）。
2. **真实交易摩擦**：支持可配置双边手续费率 ``fee_drag_pct`` 与单边滑点 ``slippage_bps``。
3. **逐根盯市统计与确定性校验**：输出 ``profit_factor``、按每根 K 线持仓状态计算的年化夏普率
   ``bar_sharpe`` 以及输入输出确定性指纹 ``determinism_hash``。
4. **统计证伪工具链**：内置固定随机种子极速置换检验 ``permutation_test``（<150ms）与
   3×3 邻域参数高原热力检验 ``parameter_plateau``。
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from datetime import date
from decimal import Decimal
from typing import Any

from kestrel.indicators.engine import Series, compute_pair
from kestrel.strategies.base import registry


class LookaheadError(RuntimeError):
    pass


def _estimate_bars_per_year(bars: list[dict]) -> float:
    """根据 K 线序列首尾日期跨度自动推断年化周期数（日线 252 / 周线 52 / 月线 12）。"""
    if len(bars) < 3:
        return 252.0
    try:
        d0 = date.fromisoformat(str(bars[0].get("date") or "")[:10])
        d1 = date.fromisoformat(str(bars[-1].get("date") or "")[:10])
        avg_days = (d1 - d0).days / max(1, len(bars) - 1)
        if avg_days >= 20.0:
            return 12.0
        if avg_days >= 4.5:
            return 52.0
    except ValueError:
        pass
    return 252.0


def _precompute_signals(
    bars: list[dict], strategy: Any, min_bars: int
) -> list[tuple[Any, dict] | None]:
    """在严格历史前缀窗口上预计算每根 K 线的信号与指标快照（供 3×3 参数网格零泄露复用）。"""
    n = len(bars)
    out: list[tuple[Any, dict] | None] = [None] * n
    for i in range(min_bars, n):
        window = bars[: i + 1]
        ind, prev = compute_pair(Series.from_bars(window))
        if not ind:
            continue
        try:
            sig = strategy.evaluate(ind, prev)
        except TypeError:
            sig = strategy.evaluate(ind)
        if sig is not None:
            out[i] = (sig, ind)
    return out


def _compute_determinism_hash(
    bars: list[dict],
    strategy_code: str,
    params: dict[str, Any],
    trades: list[dict],
    stats_core: dict[str, Any],
) -> str:
    """生成回测输入与核心结果的 SHA-256 确定性校验摘要（前 16 位十六进制）。"""
    bar_sig = {
        "n": len(bars),
        "first": (str(bars[0].get("date")), float(bars[0]["close"])) if bars else None,
        "last": (str(bars[-1].get("date")), float(bars[-1]["close"])) if bars else None,
    }
    payload = {
        "strategy": strategy_code,
        "params": params,
        "bars": bar_sig,
        "trade_rs": [t["r"] for t in trades],
        "stats": stats_core,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def run(
    bars: list[dict],
    strategy_code: str,
    max_hold: int = 15,
    min_bars: int = 60,
    registry_override: dict | None = None,
    stop_band: tuple[float, float] = (0.015, 0.15),
    fee_drag_pct: float = 0.002,
    next_open: bool = True,
    slippage_bps: float = 10.0,
    stop_atr_mult: float = 2.0,
    target_r_mult: float = 2.0,
    *,
    _precomputed_signals: list[tuple[Any, dict] | None] | None = None,
) -> dict:
    """回放规则在历史 K 线上的表现（支持次日开盘成交、可配滑点与手续费、+1R 推保本与逐根夏普）。"""
    source = registry_override if registry_override is not None else registry()
    strategy = source.get(strategy_code)
    if strategy is None:
        raise ValueError(f"unknown strategy {strategy_code}")

    slip = max(0.0, float(slippage_bps)) / 10_000.0
    fee_pct = max(0.0, float(fee_drag_pct))
    atr_mult = max(0.2, float(stop_atr_mult))
    tgt_mult = max(0.5, float(target_r_mult))
    params_meta = {
        "max_hold": int(max_hold),
        "min_bars": int(min_bars),
        "stop_band": [float(stop_band[0]), float(stop_band[1])],
        "fee_drag_pct": round(fee_pct, 6),
        "next_open": bool(next_open),
        "slippage_bps": round(float(slippage_bps), 4),
        "stop_atr_mult": round(atr_mult, 4),
        "target_r_mult": round(tgt_mult, 4),
    }

    if len(bars) <= min_bars:
        empty_st = _stats([], [], bars_per_year=_estimate_bars_per_year(bars))
        empty_st.update(
            {
                "fee_drag_pct": fee_pct,
                "slippage_bps": float(slippage_bps),
                "next_open": bool(next_open),
                "determinism_hash": _compute_determinism_hash(
                    bars, strategy_code, params_meta, [], empty_st
                ),
            }
        )
        return {
            "trades": [],
            "stats": empty_st,
            "equity": [],
            "bar_returns": [],
            "pending_signal": None,
        }

    trades: list[dict] = []
    equity: list[float] = []
    bar_returns: list[float] = []
    r_total = 0.0
    position: dict | None = None
    pending_order: dict | None = None
    pending_signal: dict | None = None
    skipped_gap_down = 0

    for i in range(min_bars, len(bars)):
        bar = bars[i]
        open_p = float(bar.get("open", bar["close"]))
        high_p = float(bar["high"])
        low_p = float(bar["low"])
        close_p = float(bar["close"])
        bar_r_delta = 0.0

        # 1. 若存在上一根收盘产生的次日开盘挂单，先在本根开盘撮合
        if position is None and pending_order is not None:
            order = pending_order
            pending_order = None
            entry = open_p * (1.0 + slip)
            stop_ref = float(order["stop_ref"])
            # 若次日开盘价已跌破或触及计划止损位，说明跳空走坏，严格放弃入场
            if open_p <= stop_ref or entry <= stop_ref:
                skipped_gap_down += 1
            else:
                risk = entry - stop_ref
                if stop_band[0] * entry <= risk <= stop_band[1] * entry:
                    target = entry + risk * tgt_mult
                    target_hint = order.get("target_hint")
                    if target_hint and float(target_hint) > entry:
                        hint_r = (float(target_hint) - entry) / risk
                        if hint_r >= 1.5:
                            target = float(target_hint)
                    position = {
                        "index": i,
                        "signal_index": order["signal_index"],
                        "date": str(bar["date"]),
                        "signal_date": order["signal_date"],
                        "entry": entry,
                        "stop": stop_ref,
                        "active_stop": stop_ref,
                        "target": target,
                        "risk": risk,
                        "mark_price": entry,
                        "entered_this_bar": True,
                    }

        # 2. 若当前持有仓位，经历本根盘中高低点与收盘状态更新
        if position is not None:
            entered_now = bool(position.pop("entered_this_bar", False))
            held = (i - position["index"] + 1) if next_open else (i - position["index"])
            fee_r = (position["entry"] * fee_pct) / position["risk"]
            entry_fee_r = fee_r * 0.5
            exit_fee_r = fee_r - entry_fee_r
            prev_mark = float(position["mark_price"])
            cur_stop = float(position["active_stop"])

            # 盘中先检查止损（开盘跳空穿仓按 open_p 真实成交并叠加卖出滑点）
            if low_p <= cur_stop:
                raw_exit = min(open_p, cur_stop) if open_p <= cur_stop else cur_stop
                exit_p = raw_exit * (1.0 - slip)
                r = (exit_p - position["entry"]) / position["risk"] - fee_r
                bar_r_delta = (exit_p - prev_mark) / position["risk"] - (
                    fee_r if entered_now else exit_fee_r
                )
                trade_rec = {k: v for k, v in position.items() if k != "mark_price"}
                trades.append({**trade_rec, "exit": exit_p, "bars": max(1, held), "r": round(r, 4)})
                r_total += r
                position = None
                equity.append(round(r_total, 4))
            elif high_p >= position["target"]:
                raw_exit = max(open_p, position["target"]) if open_p >= position["target"] else position["target"]
                exit_p = raw_exit * (1.0 - slip)
                r = (exit_p - position["entry"]) / position["risk"] - fee_r
                bar_r_delta = (exit_p - prev_mark) / position["risk"] - (
                    fee_r if entered_now else exit_fee_r
                )
                trade_rec = {k: v for k, v in position.items() if k != "mark_price"}
                trades.append({**trade_rec, "exit": exit_p, "bars": max(1, held), "r": round(r, 4)})
                r_total += r
                position = None
                equity.append(round(r_total, 4))
            elif held >= max_hold:
                exit_p = close_p * (1.0 - slip)
                r = (exit_p - position["entry"]) / position["risk"] - fee_r
                bar_r_delta = (exit_p - prev_mark) / position["risk"] - (
                    fee_r if entered_now else exit_fee_r
                )
                trade_rec = {k: v for k, v in position.items() if k != "mark_price"}
                trades.append({**trade_rec, "exit": exit_p, "bars": max(1, held), "r": round(r, 4)})
                r_total += r
                position = None
                equity.append(round(r_total, 4))
            else:
                # 持仓过夜：记录本根盯市净值波动，并在浮盈达 +1R 后将活跃止损推至保本位
                bar_r_delta = (close_p - prev_mark) / position["risk"] - (
                    entry_fee_r if entered_now else 0.0
                )
                position["mark_price"] = close_p
                if close_p >= position["entry"] + position["risk"]:
                    position["active_stop"] = max(position["active_stop"], position["entry"])
                equity.append(round(r_total, 4))

            bar_returns.append(bar_r_delta)
            continue

        # 3. 当前空仓：在历史前缀窗口 bars[:i+1] 上评估策略信号
        if _precomputed_signals is not None:
            cached = _precomputed_signals[i]
            if cached is None:
                bar_returns.append(0.0)
                continue
            signal, ind = cached
        else:
            window = bars[: i + 1]
            ind, prev = compute_pair(Series.from_bars(window))
            if not ind:
                bar_returns.append(0.0)
                continue
            try:
                signal = strategy.evaluate(ind, prev)
            except TypeError:
                signal = strategy.evaluate(ind)
            if signal is None:
                bar_returns.append(0.0)
                continue

        ref_close = close_p
        atr_val = float(ind.get("atr14") or 0)
        atr_stop = ref_close - atr_val * atr_mult if atr_val > 0 else ref_close * 0.97
        stop_ref = atr_stop
        if signal.stop_hint and 0 < float(signal.stop_hint) < ref_close:
            stop_ref = max(stop_ref, float(signal.stop_hint))
        stop_ref = min(stop_ref, ref_close * 0.99)
        if stop_ref >= ref_close:
            bar_returns.append(0.0)
            continue

        if next_open:
            if i == len(bars) - 1:
                # 最后一根 K 线刚触发信号，尚无次日开盘价，返回挂起提示而不虚构持仓
                pending_signal = {
                    "signal_index": i,
                    "signal_date": str(bar["date"]),
                    "ref_close": round(ref_close, 4),
                    "stop_ref": round(stop_ref, 4),
                    "score": round(float(getattr(signal, "score", 0.0) or 0.0), 4),
                }
            else:
                pending_order = {
                    "signal_index": i,
                    "signal_date": str(bar["date"]),
                    "stop_ref": stop_ref,
                    "target_hint": getattr(signal, "target_hint", None),
                }
            bar_returns.append(0.0)
        else:
            # 兼容模式（next_open=False）：信号当根收盘叠加滑点入场
            entry = ref_close * (1.0 + slip)
            if stop_ref >= entry:
                bar_returns.append(0.0)
                continue
            risk = entry - stop_ref
            if not (stop_band[0] * entry <= risk <= stop_band[1] * entry):
                bar_returns.append(0.0)
                continue
            target = entry + risk * tgt_mult
            if signal.target_hint and float(signal.target_hint) > entry:
                hint_r = (float(signal.target_hint) - entry) / risk
                if hint_r >= 1.5:
                    target = float(signal.target_hint)
            fee_r = (entry * fee_pct) / risk
            entry_fee_r = fee_r * 0.5
            position = {
                "index": i,
                "signal_index": i,
                "date": str(bar["date"]),
                "signal_date": str(bar["date"]),
                "entry": entry,
                "stop": stop_ref,
                "active_stop": stop_ref,
                "target": target,
                "risk": risk,
                "mark_price": ref_close,
                "entered_this_bar": False,
            }
            bar_returns.append((ref_close - entry) / risk - entry_fee_r)

    # 序列结束仍有未平仓头寸时，按最后一根收盘扣滑点与剩余费用结算
    if position is not None:
        last_close = float(bars[-1]["close"])
        exit_p = last_close * (1.0 - slip)
        fee_r = (position["entry"] * fee_pct) / position["risk"]
        exit_fee_r = fee_r - fee_r * 0.5
        prev_mark = float(position["mark_price"])
        r = (exit_p - position["entry"]) / position["risk"] - fee_r
        held_bars = (
            (len(bars) - position["index"])
            if next_open
            else max(1, len(bars) - 1 - position["index"])
        )
        trade_rec = {k: v for k, v in position.items() if k != "mark_price"}
        trades.append(
            {
                **trade_rec,
                "exit": exit_p,
                "bars": max(1, held_bars),
                "r": round(r, 4),
            }
        )
        if bar_returns:
            bar_returns[-1] += (exit_p - prev_mark) / position["risk"] - exit_fee_r
        equity.append(round(r_total + r, 4))

    ann_bars = _estimate_bars_per_year(bars)
    stats = _stats(trades, bar_returns, bars_per_year=ann_bars)
    stats.update(
        {
            "fee_drag_pct": fee_pct,
            "slippage_bps": float(slippage_bps),
            "next_open": bool(next_open),
            "skipped_gap_down": skipped_gap_down,
        }
    )
    stats["determinism_hash"] = _compute_determinism_hash(
        bars, strategy_code, params_meta, trades, stats
    )
    return {
        "trades": trades,
        "stats": stats,
        "equity": equity,
        "bar_returns": [round(x, 6) for x in bar_returns],
        "pending_signal": pending_signal,
    }


def _stats(
    trades: list[dict],
    bar_returns: list[float] | None = None,
    *,
    bars_per_year: float = 252.0,
) -> dict:
    """汇总交易级与逐根 K 线级风险收益统计指标。"""
    bar_sharpe = 0.0
    if bar_returns and len(bar_returns) >= 2:
        n_bars = len(bar_returns)
        mean_b = sum(bar_returns) / n_bars
        var_b = sum((x - mean_b) ** 2 for x in bar_returns) / (n_bars - 1)
        std_b = math.sqrt(var_b)
        if std_b > 1e-12:
            bar_sharpe = (mean_b / std_b) * math.sqrt(bars_per_year)

    if not trades:
        return {
            "count": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "total_r": 0.0,
            "max_dd": 0.0,
            "profit_factor": 0.0,
            "bar_sharpe": round(bar_sharpe, 3),
        }

    wins = [t["r"] for t in trades if t["r"] > 0]
    losses = [t["r"] for t in trades if t["r"] <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (
        round(gross_win / gross_loss, 3)
        if gross_loss > 1e-12
        else (99.0 if gross_win > 0 else 0.0)
    )

    total_r = sum(t["r"] for t in trades)
    peak = 0.0
    max_dd = 0.0
    running = 0.0
    for t in trades:
        running += t["r"]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    return {
        "count": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_r": total_r / len(trades),
        "total_r": total_r,
        "max_dd": max_dd,
        "profit_factor": profit_factor,
        "bar_sharpe": round(bar_sharpe, 3),
    }


def _classify_p_value(p_val: float) -> tuple[str, str, str]:
    """根据经验 p 值输出显著性等级与中英文结论。"""
    if p_val < 0.01:
        return (
            "strong",
            "强显著 (p < 0.01)",
            "Strongly significant (p < 0.01)",
        )
    if p_val < 0.05:
        return (
            "significant",
            "显著 (p < 0.05)",
            "Significant (p < 0.05)",
        )
    return (
        "noise",
        "无法排除随机运气 (p >= 0.05)",
        "Cannot rule out luck (p >= 0.05)",
    )


def permutation_test(
    bars_or_trades: list[dict],
    strategy_code: str | None = None,
    *,
    n_perms: int = 500,
    seed: int = 42,
    max_hold: int = 15,
    min_bars: int = 60,
    fee_drag_pct: float = 0.002,
    slippage_bps: float = 10.0,
    stop_atr_mult: float = 2.0,
    target_r_mult: float = 2.0,
    stop_band: tuple[float, float] = (0.015, 0.15),
    registry_override: dict | None = None,
    precomputed_result: dict | None = None,
    benchmark_rs: list[float] | None = None,
) -> dict:
    """固定随机种子置换检验（保持总体涨跌幅度分布而随机打乱顺序，检验择时显著性）。

    支持两种输入形态：
    1) ``bars_or_trades`` 为 K 线序列（含 ``close``），配合 ``strategy_code``（或 ``precomputed_result``）：
       保留全区间单根涨跌幅度分布，打乱时间顺序后按同等持仓跨度与止损止盈边界模拟零分布。
    2) ``bars_or_trades`` 为跨标的样本外交易列表（含 ``r``）：
       对超额收益/收益幅度分布做固定种子随机置换，检验正期望是否显著异于零假设。
    """
    t0 = time.perf_counter()
    n_perms = max(50, int(n_perms))
    rng = random.Random(seed)

    if not bars_or_trades:
        level, zh, en = _classify_p_value(1.0)
        return {
            "observed": 0.0,
            "p_value": 1.0,
            "null_mean": 0.0,
            "null_p95": 0.0,
            "n_perms": n_perms,
            "seed": seed,
            "trades": 0,
            "level": level,
            "significant": False,
            "verdict_zh": zh,
            "verdict_en": en,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    is_bars = "close" in bars_or_trades[0]

    if is_bars:
        bars = bars_or_trades
        if precomputed_result is not None:
            real = precomputed_result
        else:
            if not strategy_code:
                raise ValueError("strategy_code is required when bars are passed")
            real = run(
                bars,
                strategy_code,
                max_hold=max_hold,
                min_bars=min_bars,
                registry_override=registry_override,
                stop_band=stop_band,
                fee_drag_pct=fee_drag_pct,
                slippage_bps=slippage_bps,
                stop_atr_mult=stop_atr_mult,
                target_r_mult=target_r_mult,
            )
        trades = real["trades"]
        k_trades = len(trades)
        if k_trades == 0 or len(bars) <= min_bars + 2:
            level, zh, en = _classify_p_value(1.0)
            return {
                "observed": 0.0,
                "p_value": 1.0,
                "null_mean": 0.0,
                "null_p95": 0.0,
                "n_perms": n_perms,
                "seed": seed,
                "trades": 0,
                "level": level,
                "significant": False,
                "verdict_zh": zh,
                "verdict_en": en,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

        observed = float(real["stats"]["avg_r"])
        # 计算每笔交易的平均 1R 价格风险比例，将每根 K 线真实涨跌幅归一化为 R 单位幅度池
        risk_pcts = [
            float(t["risk"]) / float(t["entry"])
            for t in trades
            if float(t.get("entry") or 0) > 0 and float(t.get("risk") or 0) > 0
        ]
        mean_risk_pct = (sum(risk_pcts) / len(risk_pcts)) if risk_pcts else 0.03
        mean_risk_pct = max(0.005, mean_risk_pct)

        total_cost_pct = max(0.0, float(fee_drag_pct)) + 2.0 * max(0.0, float(slippage_bps)) / 10_000.0
        friction_r = total_cost_pct / mean_risk_pct

        # 提取评估区间内所有单根 K 线的真实涨跌幅度（保留完整分布与肥尾特征）
        start_idx = max(1, min_bars)
        step_r_pool = [
            ((float(bars[j]["close"]) - float(bars[j - 1]["close"])) / float(bars[j - 1]["close"]))
            / mean_risk_pct
            for j in range(start_idx, len(bars))
            if float(bars[j - 1]["close"]) > 0
        ]
        if not step_r_pool:
            step_r_pool = [0.0]

        hold_lengths = [max(1, int(t.get("bars") or 1)) for t in trades]
        pool_len = len(step_r_pool)
        upper_cap = float(target_r_mult)
        lower_cap = -1.0

        null_dist: list[float] = []
        shuffled = list(step_r_pool)
        for _ in range(n_perms):
            rng.shuffle(shuffled)
            cursor = 0
            sim_total = 0.0
            for h_len in hold_lengths:
                cum_r = 0.0
                for step in range(h_len):
                    cum_r += shuffled[(cursor + step) % pool_len]
                    if cum_r <= lower_cap:
                        cum_r = lower_cap
                        break
                    if cum_r >= upper_cap:
                        cum_r = upper_cap
                        break
                cursor = (cursor + h_len) % pool_len
                sim_total += cum_r - friction_r
            null_dist.append(sim_total / k_trades)
    else:
        # 交易列表模式（用于跨标的 Walk-Forward 汇总显著性证伪）
        trades = bars_or_trades
        k_trades = len(trades)
        rs = [float(t["r"]) for t in trades]
        if benchmark_rs and len(benchmark_rs) == k_trades:
            diffs = [r_val - float(b_val) for r_val, b_val in zip(rs, benchmark_rs, strict=False)]
        else:
            diffs = [
                float(t["r"]) - float(t.get("bench_r", 0.0) or 0.0)
                for t in trades
            ]
        observed = sum(diffs) / k_trades
        # 保留所有单笔幅度绝对值分布，在零假设下随机分配方向与排列顺序
        magnitudes = [abs(x) for x in diffs]
        null_dist = []
        work = list(magnitudes)
        for _ in range(n_perms):
            rng.shuffle(work)
            sim_sum = 0.0
            for mag in work:
                sim_sum += mag if rng.random() < 0.5 else -mag
            null_dist.append(sim_sum / k_trades)

    null_dist.sort()
    exceed = sum(1 for val in null_dist if val >= observed)
    p_value = (exceed + 1) / (n_perms + 1)
    null_mean = sum(null_dist) / len(null_dist)
    p95_idx = min(len(null_dist) - 1, int(0.95 * (len(null_dist) - 1)))
    null_p95 = null_dist[p95_idx]
    level, zh, en = _classify_p_value(p_value)

    return {
        "observed": round(observed, 4),
        "p_value": round(p_value, 4),
        "null_mean": round(null_mean, 4),
        "null_p95": round(null_p95, 4),
        "n_perms": n_perms,
        "seed": seed,
        "trades": k_trades,
        "level": level,
        "significant": bool(p_value < 0.05),
        "verdict_zh": zh,
        "verdict_en": en,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
    }


def parameter_plateau(
    bars: list[dict],
    strategy_code: str,
    base_hold: int = 15,
    fee_drag_pct: float = 0.002,
    slippage_bps: float = 10.0,
    *,
    min_bars: int = 60,
    stop_band: tuple[float, float] = (0.015, 0.15),
    registry_override: dict | None = None,
    next_open: bool = True,
) -> dict:
    """扫描 max_hold (3 档) × stop_atr_mult (3 档) 的 3×3 邻域网格，识别稳健参数高原 vs 孤立过拟合尖刺。"""
    source = registry_override if registry_override is not None else registry()
    strategy = source.get(strategy_code)
    if strategy is None:
        raise ValueError(f"unknown strategy {strategy_code}")

    low_hold = max(3, round(base_hold * 0.7))
    mid_hold = max(3, int(base_hold))
    high_hold = max(mid_hold + 1, round(base_hold * 1.3))
    if low_hold >= mid_hold:
        low_hold = max(2, mid_hold - 1)
    hold_steps = [low_hold, mid_hold, high_hold]
    atr_steps = [1.5, 2.0, 2.5]

    precomputed = _precompute_signals(bars, strategy, min_bars) if len(bars) > min_bars else None

    grid: list[list[dict]] = []
    cells: list[dict] = []
    for h_val in hold_steps:
        row_cells: list[dict] = []
        for a_val in atr_steps:
            res = run(
                bars,
                strategy_code,
                max_hold=h_val,
                min_bars=min_bars,
                registry_override=source,
                stop_band=stop_band,
                fee_drag_pct=fee_drag_pct,
                next_open=next_open,
                slippage_bps=slippage_bps,
                stop_atr_mult=a_val,
                _precomputed_signals=precomputed,
            )
            st = res["stats"]
            cell = {
                "max_hold": h_val,
                "stop_atr_mult": a_val,
                "is_center": bool(h_val == mid_hold and abs(a_val - 2.0) < 1e-6),
                "trades": st["count"],
                "avg_r": round(st["avg_r"], 4),
                "total_r": round(st["total_r"], 4),
                "win_rate": round(st["win_rate"], 4),
                "profit_factor": st["profit_factor"],
                "bar_sharpe": st["bar_sharpe"],
                "positive": bool(st["count"] > 0 and st["avg_r"] > 0),
            }
            row_cells.append(cell)
            cells.append(cell)
        grid.append(row_cells)

    positive_count = sum(1 for c in cells if c["positive"])
    plateau_ratio = round(positive_count / 9.0, 4)
    avg_across = sum(c["avg_r"] for c in cells) / 9.0
    best_avg_r = max(c["avg_r"] for c in cells)
    center_cell = grid[1][1]

    if positive_count >= 6 and avg_across > 0:
        shape = "plateau"
        label_zh = "稳健参数高原（邻域多数正期望）"
        label_en = "Robust parameter plateau"
    elif positive_count > 0 and best_avg_r > 0:
        shape = "spike"
        label_zh = "孤立尖刺警告（疑似参数过拟合）"
        label_en = "Isolated spike warning (overfit risk)"
    else:
        shape = "weak"
        label_zh = "整体无优势（邻域无正期望）"
        label_en = "Weak across neighborhood (no edge)"

    return {
        "hold_steps": hold_steps,
        "atr_steps": atr_steps,
        "grid": grid,
        "cells": cells,
        "center": center_cell,
        "positive_cells": positive_count,
        "total_cells": 9,
        "plateau_ratio": plateau_ratio,
        "neighborhood_avg_r": round(avg_across, 4),
        "shape": shape,
        "label_zh": label_zh,
        "label_en": label_en,
    }


def save(
    market: str,
    code: str,
    strategy: str,
    result: dict,
    start: str,
    end: str,
    timeframe: str = "short",
) -> int:
    from kestrel.db.engine import connect

    stats = result["stats"]
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO backtest_run (market, code, strategy, start_date, end_date,
                                      trades, win_rate, avg_r, max_dd, equity, timeframe)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market,
                code,
                strategy,
                start,
                end,
                stats["count"],
                stats["win_rate"],
                stats["avg_r"],
                stats["max_dd"],
                json.dumps(result["equity"][-120:]),
                timeframe,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent(limit: int = 20) -> list[dict]:
    from kestrel.db.engine import connect

    conn = connect()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM backtest_run ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]
    finally:
        conn.close()


def to_decimal(value: float) -> Decimal:
    return Decimal(str(round(value, 4)))
