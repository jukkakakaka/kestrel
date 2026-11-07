"""Walk-forward validation with a benchmark, because "it went up" is not skill.

Every rule here is long-only. In a rising market they all make money, so a raw
expectancy number proves nothing. This script therefore reports, per fold:

* the rule's expectancy inside the fold,
* the same window's buy-and-hold return, so beta can be subtracted,
* whether the rule beat holding the stock.

Three consecutive folds show whether a result survives more than one regime.

Usage:
    uv run python scripts/validate_rules.py [--folds 3] [--symbols 200]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kestrel.db.engine import connect  # noqa: E402
from kestrel.services import backtest  # noqa: E402
from kestrel.services import bars as bars_service  # noqa: E402
from kestrel.strategies import base as base_module  # noqa: E402
from kestrel.strategies import library  # noqa: E402
from kestrel.strategies.spec import compile_rule  # noqa: E402

WARMUP = 80
MIN_TRADES = 12


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "win": 0.0, "exp": 0.0, "total": 0.0}
    wins = [t for t in trades if t["r"] > 0]
    total = sum(t["r"] for t in trades)
    return {
        "n": len(trades),
        "win": len(wins) / len(trades) * 100,
        "exp": total / len(trades),
        "total": total,
    }


def _hold_r(chunk: list[dict], trade: dict) -> float | None:
    """What the same trade would have returned with no stop and no target.

    Same entry bar, same holding period, expressed in the same R units. This is
    the only fair comparison: if a rule cannot beat doing nothing for as long,
    its stops and targets are not adding value.
    """
    i = trade["index"]
    held = max(1, int(trade.get("bars") or 1))
    j = min(len(chunk) - 1, i + held)
    if j <= i or not trade.get("risk"):
        return None
    entry = float(chunk[i]["close"])
    exit_price = float(chunk[j]["close"])
    return (exit_price - entry) / float(trade["risk"])


def _folds(bars: list[dict], count: int) -> list[tuple[int, int]]:
    """Split into ``count`` consecutive test windows with a warmup before each."""
    if len(bars) < WARMUP * 2:
        return []
    start = WARMUP + int((len(bars) - WARMUP) * 0.35)
    step = max(WARMUP, (len(bars) - start) // count)
    out = []
    for i in range(count):
        lo = start + i * step
        hi = min(len(bars), lo + step)
        if hi - lo < 40:
            break
        out.append((lo, hi))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--symbols", type=int, default=200)
    args = parser.parse_args()

    conn = connect()
    rows = conn.execute(
        """
        SELECT id, market, code FROM symbol
        WHERE is_pool=1 OR id IN (SELECT symbol_id FROM watchlist)
        ORDER BY is_pool DESC, pool_rank LIMIT ?
        """,
        (args.symbols,),
    ).fetchall()
    universe: list[list[dict]] = []
    for row in rows:
        bars = bars_service.cached_bars(conn, row["id"], 700)
        if len(bars) >= WARMUP * 2 + 80:
            universe.append(bars)
    conn.close()

    if not universe:
        print("没有足够的历史数据。先跑一次扫描缓存日线。")
        return 1

    compiled = {}
    for item in library.RULES:
        rule = compile_rule(item["spec"])
        compiled[rule.code] = rule
        if rule.code not in {r.code for r in base_module.BUILTIN}:
            base_module.BUILTIN.append(rule)

    print(f"标的 {len(universe)} 只，{args.folds} 折滚动，每折都有独立的买入持有基准\n")

    summary = []
    for code, rule in compiled.items():
        fold_rows = []
        for f in range(args.folds):
            trades: list[dict] = []
            bench: list[float] = []
            for bars in universe:
                windows = _folds(bars, args.folds)
                if f >= len(windows):
                    continue
                lo, hi = windows[f]
                slice_start = max(0, lo - WARMUP)
                chunk = bars[slice_start:hi]
                offset = lo - slice_start
                try:
                    res = backtest.run(chunk, code)
                except Exception:  # noqa: BLE001
                    continue
                for t in res["trades"]:
                    if t["index"] < offset:
                        continue
                    trades.append(t)
                    hold = _hold_r(chunk, t)
                    if hold is not None:
                        bench.append(hold)
            st = _stats(trades)
            avg_bench = sum(bench) / len(bench) if bench else 0.0
            fold_rows.append({**st, "bench": avg_bench})

        traded = [r for r in fold_rows if r["n"] >= MIN_TRADES]
        if not traded:
            summary.append((rule.name, code, fold_rows, "样本不足", 0.0, 0.0))
            continue
        exp = sum(r["exp"] for r in traded) / len(traded)
        positive = sum(1 for r in traded if r["exp"] > 0)
        # a rule must clear zero in most folds AND not depend on one lucky window
        beat = sum(1 for r in traded if r["exp"] > r["bench"])
        verdict = (
            "通过" if positive == len(traded) and exp > 0.05 and beat == len(traded)
            else "不稳" if exp > 0 else "不通过"
        )
        summary.append((rule.name, code, fold_rows, verdict, exp, positive / len(traded)))

    header = f"{'规则':<16}{'折':>3}{'笔数':>7}{'胜率':>8}{'每笔期望':>10}{'不动持有':>10}{'超额':>8}"
    print(header)
    print("-" * 62)
    for name, _code, folds, verdict, _exp, _hit in summary:
        for i, row in enumerate(folds):
            if row["n"] == 0:
                continue
            edge = row["exp"] - row["bench"]
            print(
                f"{(name if i == 0 else ''):<16}{i + 1:>3}{row['n']:>7}{row['win']:>7.1f}%"
                f"{row['exp']:>+10.3f}{row['bench']:>+10.3f}{edge:>+8.3f}"
            )
        print(f"{'':<16}{'判定':>3}  {verdict}\n")

    print("=" * 62)
    passed = [s for s in summary if s[3] == "通过"]
    print(f"通过 {len(passed)} / {len(summary)}：")
    for name, _c, _f, _v, exp, hit in sorted(passed, key=lambda s: -s[4]):
        print(f"  ✓ {name:<10} 平均每笔 {exp:+.3f}R，{hit:.0%} 的折为正")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
