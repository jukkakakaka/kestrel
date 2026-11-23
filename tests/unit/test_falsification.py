"""量化证伪引擎单元测试：次日开盘防前视、置换检验、3×3 参数高原、四关证伪与实盘偏离监控。"""

from __future__ import annotations

import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from kestrel.datasources import resolve as resolver
from kestrel.datasources.base import normalize_code
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.fees import plans as plan_service
from kestrel.services import backtest, copilot, execution, glossary, journal, rules
from kestrel.services import bars as bars_service
from kestrel.settings import settings
from kestrel.strategies.base import StrategyResult


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(settings, "home", home)
    monkeypatch.setenv("KESTREL_AUTOSTART_DIR", str(tmp_path / "autostart"))
    settings.ensure_dirs()
    init_db()
    plan_service.hydrate()
    glossary.seed()
    rules.ensure_seeded()
    return home


def _make_bars(n: int = 160, start_price: float = 100.0, drift: float = 0.35) -> list[dict]:
    """生成带温和波动与可控趋势斜率的确定性 OHLCV 日线序列。"""
    out: list[dict] = []
    px = start_price
    d0 = date(2025, 1, 2)
    for idx in range(n):
        wave = ((idx % 7) - 3) * 0.45
        open_p = max(5.0, px + (0.12 if idx % 2 == 0 else -0.08))
        close_p = max(5.0, open_p + drift + wave)
        high_p = max(open_p, close_p) + 1.2
        low_p = min(open_p, close_p) - 1.1
        out.append(
            {
                "date": str(d0 + timedelta(days=idx)),
                "open": round(open_p, 4),
                "high": round(high_p, 4),
                "low": round(low_p, 4),
                "close": round(close_p, 4),
                "volume": 100_000 + (idx % 5) * 15_000,
                "turnover": round(close_p * 100_000, 2),
            }
        )
        px = close_p
    return out


class _TriggerAtIndexStrategy:
    """仅在指定 K 线下标集合触发信号的测试桩策略。"""

    code = "stub_idx"
    name = "定点触发桩"

    def __init__(self, target_bars_lengths: set[int]) -> None:
        # compute(window) 里的 ind["bars"] == len(window) == i + 1
        self._lengths = target_bars_lengths

    def evaluate(self, ind: dict, _prev: dict | None = None):
        if int(ind.get("bars") or 0) in self._lengths:
            return StrategyResult(code=self.code, score=0.8, reasons=["stub"])
        return None


# ------------------------------------------------------------------ 1. 次日开盘成交、跳空破止损放弃、末尾挂起信号与确定性哈希


def test_next_open_eliminates_lookahead_skips_gap_down_and_tracks_pending():
    bars = _make_bars(100, start_price=100.0, drift=0.2)
    # 在 i=65 收盘触发信号 -> 应在 i=66 的 open 叠加 10 bps 滑点买入
    bars[66]["open"] = 115.0
    bars[66]["high"] = 118.0
    bars[66]["low"] = 114.0
    bars[66]["close"] = 117.0

    reg = {"stub_idx": _TriggerAtIndexStrategy({66})}  # i=65 时 len(bars[:66]) == 66
    res1 = backtest.run(
        bars,
        "stub_idx",
        max_hold=5,
        min_bars=60,
        registry_override=reg,
        fee_drag_pct=0.002,
        next_open=True,
        slippage_bps=10.0,
    )
    res2 = backtest.run(
        bars,
        "stub_idx",
        max_hold=5,
        min_bars=60,
        registry_override=reg,
        fee_drag_pct=0.002,
        next_open=True,
        slippage_bps=10.0,
    )

    assert len(res1["trades"]) == 1
    tr = res1["trades"][0]
    assert tr["signal_index"] == 65
    assert tr["index"] == 66
    assert tr["entry"] == pytest.approx(115.0 * 1.001, rel=1e-6)
    assert "profit_factor" in res1["stats"]
    assert "bar_sharpe" in res1["stats"]
    assert len(res1["stats"]["determinism_hash"]) == 16
    assert res1["stats"]["determinism_hash"] == res2["stats"]["determinism_hash"]

    # 若次日（i=66）开盘直接跳空暴跌跌破止损位，必须放弃入场而不强行成交
    gap_bars = _make_bars(100, start_price=100.0, drift=0.2)
    gap_bars[66]["open"] = 60.0
    gap_bars[66]["low"] = 58.0
    gap_bars[66]["high"] = 62.0
    gap_bars[66]["close"] = 61.0
    res_gap = backtest.run(
        gap_bars,
        "stub_idx",
        max_hold=5,
        min_bars=60,
        registry_override=reg,
        next_open=True,
    )
    assert res_gap["trades"] == []
    assert res_gap["stats"]["skipped_gap_down"] == 1

    # 若最后一根 K 线（i=99, len=100）刚触发信号，无未成交仓位且返回 pending_signal
    reg_last = {"stub_idx": _TriggerAtIndexStrategy({100})}
    res_last = backtest.run(
        bars,
        "stub_idx",
        max_hold=5,
        min_bars=60,
        registry_override=reg_last,
        next_open=True,
    )
    assert res_last["trades"] == []
    assert res_last["pending_signal"] is not None
    assert res_last["pending_signal"]["signal_index"] == 99


# ------------------------------------------------------------------ 2. 固定种子置换检验（<150ms、确定性、显著性分级）


def test_permutation_test_speed_determinism_and_significance():
    bars = _make_bars(180, start_price=80.0, drift=0.4)
    # 每隔 12 根触发一次的规律策略
    trigger_lens = set(range(65, 175, 12))
    reg = {"stub_idx": _TriggerAtIndexStrategy(trigger_lens)}

    t0 = time.perf_counter()
    perm1 = backtest.permutation_test(
        bars,
        "stub_idx",
        n_perms=500,
        seed=42,
        max_hold=6,
        registry_override=reg,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    perm2 = backtest.permutation_test(
        bars,
        "stub_idx",
        n_perms=500,
        seed=42,
        max_hold=6,
        registry_override=reg,
    )

    assert elapsed_ms < 150.0, f"500 次置换耗时 {elapsed_ms:.1f}ms 超过 150ms 预算"
    assert perm1["p_value"] == perm2["p_value"]
    assert perm1["null_mean"] == perm2["null_mean"]
    assert perm1["null_p95"] == perm2["null_p95"]
    assert 0.0 < perm1["p_value"] <= 1.0

    # 对交易列表进行强优势 vs 随机噪声显著性分级检验
    strong_trades = [{"r": 1.6} for _ in range(18)] + [{"r": -0.5} for _ in range(3)]
    p_strong = backtest.permutation_test(strong_trades, n_perms=500, seed=42)
    assert p_strong["p_value"] < 0.01
    assert p_strong["level"] == "strong"
    assert p_strong["significant"] is True

    noise_trades = [{"r": 0.8}, {"r": -0.9}] * 10
    p_noise = backtest.permutation_test(noise_trades, n_perms=500, seed=42)
    assert p_noise["p_value"] >= 0.05
    assert p_noise["level"] == "noise"
    assert p_noise["significant"] is False


# ------------------------------------------------------------------ 3. 3×3 参数高原 vs 孤立尖刺检验


def test_parameter_plateau_identifies_plateau_spike_and_weak(monkeypatch):
    bars = _make_bars(140, start_price=50.0, drift=0.55)
    reg = {"stub_idx": _TriggerAtIndexStrategy(set(range(65, 130, 10)))}

    # 1) 稳健上行趋势：3×3 邻域普遍正期望 -> plateau
    plat = backtest.parameter_plateau(
        bars, "stub_idx", base_hold=15, registry_override=reg
    )
    assert plat["hold_steps"] == [10, 15, 20]
    assert plat["atr_steps"] == [1.5, 2.0, 2.5]
    assert len(plat["grid"]) == 3 and all(len(r) == 3 for r in plat["grid"])
    assert plat["shape"] == "plateau"
    assert plat["plateau_ratio"] >= 6 / 9

    # 2) 下跌无优势行情 -> weak
    down_bars = _make_bars(140, start_price=150.0, drift=-0.6)
    weak = backtest.parameter_plateau(
        down_bars, "stub_idx", base_hold=15, registry_override=reg
    )
    assert weak["shape"] == "weak"
    assert weak["plateau_ratio"] == 0.0

    # 3) 仅单点参数正期望、周围邻域全负 -> 识别为孤立过拟合尖刺 spike
    orig_run = backtest.run

    def _spiky_run(b, code, max_hold=15, **kw):
        res = orig_run(b, code, max_hold=max_hold, **kw)
        is_exact = max_hold == 15 and abs(float(kw.get("stop_atr_mult", 2.0)) - 2.0) < 1e-6
        fake_r = 0.85 if is_exact else -0.45
        res["stats"]["count"] = 10
        res["stats"]["avg_r"] = fake_r
        res["stats"]["total_r"] = fake_r * 10
        return res

    monkeypatch.setattr(backtest, "run", _spiky_run)
    spike = backtest.parameter_plateau(
        bars, "stub_idx", base_hold=15, registry_override=reg
    )
    assert spike["shape"] == "spike"
    assert spike["positive_cells"] == 1


# ------------------------------------------------------------------ 4. 四关证伪串联与 p >= 0.10 降级守卫 + 第 4 关实盘偏离预警


def test_rules_validate_four_gates_and_pvalue_downgrade(isolated_home, monkeypatch):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size, is_pool, pool_rank)"
            " VALUES ('CN', '600519', '贵州茅台', 'CNY', 100, 1, 1)"
        )
        sid = conn.execute("SELECT id FROM symbol WHERE code='600519'").fetchone()["id"]
        bars_service.save_bars(conn, sid, _make_bars(260, start_price=100.0, drift=0.3))
        conn.commit()
    finally:
        conn.close()

    # 模拟滚动前推每折都有 >=12 笔且正期望跑赢基准，但置换检验 p_value = 0.24 (>= 0.10)
    def _fake_bt_run(chunk, code, **kw):
        fake_trades = [
            {"index": len(chunk) - 5, "date": "2025-06-01", "bars": 2, "risk": 2.0, "r": 0.45}
            for _ in range(14)
        ]
        return {"trades": fake_trades, "stats": backtest._stats(fake_trades), "equity": [0.45]}

    monkeypatch.setattr(backtest, "run", _fake_bt_run)
    monkeypatch.setattr(rules, "_hold_r", lambda chunk, tr: 0.10)
    monkeypatch.setattr(
        backtest,
        "permutation_test",
        lambda *a, **kw: {
            "observed": 0.35,
            "p_value": 0.24,
            "null_mean": 0.0,
            "null_p95": 0.42,
            "n_perms": 500,
            "seed": 42,
            "trades": 42,
            "level": "noise",
            "significant": False,
            "verdict_zh": "无法排除随机运气 (p >= 0.05)",
            "verdict_en": "Cannot rule out luck (p >= 0.05)",
            "elapsed_ms": 2.1,
        },
    )

    out = rules.validate("trend_pullback", folds=3, horizon="short", persist=True)
    assert out["ok"] is True
    assert out["downgraded_by_perm"] is True
    assert out["verdict"] == "不稳定", "p >= 0.10 时绝不能盲目判为有优势"
    assert len(out["gates"]) == 4
    assert [g["code"] for g in out["gates"]] == [
        "cost_sample",
        "walk_forward",
        "permutation",
        "live_drift",
    ]

    # 验证持久化后从数据库读回时，validation['folds'] 仍为列表（100% 向后兼容）且携带 p_value
    loaded = next(r for r in rules.all_rules() if r["code"] == "trend_pullback")
    assert isinstance(loaded["validation"]["folds"], list)
    assert loaded["validation"]["p_value"] == pytest.approx(0.24)
    assert len(loaded["gates"]) == 4


def test_live_drift_report_detects_underperformance(isolated_home):
    # 写入一条回测 avg_exp = +0.55R 的验证记录
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO rule_validation (code, verdict, avg_exp, avg_edge, folds, symbols, timeframe)
            VALUES ('trend_pullback', '有优势', 0.55, 0.25, '[]', 30, 'short')
            """
        )
        conn.commit()
    finally:
        conn.close()

    # 尚未满 3 笔已平仓交易时，状态为 insufficient 且不误报 warning
    d0 = rules.live_drift_report("trend_pullback")["trend_pullback"]
    assert d0["status"] == "insufficient"
    assert d0["warning"] is False

    # 录入 3 笔亏损平仓交易（买入 100，止损 95 即 1R=5，平仓在 95.5 约 -0.9R，远低于 +0.55R）
    for idx in range(3):
        tid = journal.add_trade(
            market="US",
            code=f"T{idx}",
            name=f"Test{idx}",
            qty=Decimal("10"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            strategy="trend_pullback",
            opened_at="2025-05-01T10:00:00+00:00",
        )
        journal.close_trade(tid, "95.5", review="止损离场")

    d1 = rules.live_drift_report("trend_pullback")["trend_pullback"]
    assert d1["sample_size"] == 3
    assert d1["actual_avg_r"] < -0.8
    assert d1["status"] == "warning"
    assert d1["warning"] is True
    assert "表现偏离预警" in d1["verdict_zh"]


# ------------------------------------------------------------------ 5. Copilot 解耦铁律 + /sim、/rules、/journal 端到端集成


def test_copilot_context_and_prompt_enforce_numerical_honesty(isolated_home):
    ctx = copilot.build_context(page_path="/rules", locale="zh-CN")
    assert "strategy_falsification" in ctx
    assert "recent_backtests" in ctx
    assert len(ctx["strategy_falsification"]) >= 3

    prompt_zh = copilot._system_prompt("zh-CN", ctx)
    prompt_en = copilot._system_prompt("en", ctx)
    assert "Python 算数字，AI 做研究" in prompt_zh
    assert "未验证/样本不足" in prompt_zh
    assert "NEVER fabricate" in prompt_en
    assert "Unvalidated / Insufficient sample" in prompt_en


def test_sim_and_rules_routes_and_journal_equity_curve_regression(isolated_home, monkeypatch):
    from kestrel.app import create_app

    # 播种 1 只充足 K 线的美股标的以测试 POST /sim 全链路证伪输出
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size)"
            " VALUES ('US', 'AAPL', 'Apple', 'USD', 1)"
        )
        sid = conn.execute("SELECT id FROM symbol WHERE code='AAPL'").fetchone()["id"]
        bars_service.save_bars(conn, sid, _make_bars(220, start_price=150.0, drift=0.35))
        conn.commit()
    finally:
        conn.close()

    class _Found:
        market = "US"
        code = normalize_code("US", "AAPL")
        name = "Apple"
        exchange = "NASDAQ"
        secid = "105.AAPL"
        currency = "USD"
        lot_size = 1

    async def _fake_resolve(code, market=None):
        return _Found()

    monkeypatch.setattr(resolver, "resolve", _fake_resolve)

    # 录入并平掉一笔交易，回归测试 journal.equity_curve() 与 GET /journal 不抛 KeyError: 'status'
    res_buy = execution.record(
        execution.Fill(
            market="US",
            code="AAPL",
            side="buy",
            qty=Decimal("5"),
            price=Decimal("180"),
            stop=Decimal("170"),
            name="Apple",
        )
    )
    assert res_buy["ok"] is True
    open_rows = journal.list_trades("open")
    assert len(open_rows) == 1
    journal.close_trade(open_rows[0]["id"], "192", review="按计划止盈")

    curve = journal.equity_curve()
    assert len(curve) == 1
    assert curve[0]["net_cny"] > 0

    with TestClient(create_app()) as client:
        r_journal = client.get("/journal")
        assert r_journal.status_code == 200

        r_sim = client.post(
            "/sim",
            data={
                "symbol": "US/AAPL",
                "strategy": "trend_pullback",
                "horizon": "short",
                "fee_bps": "25",
                "slippage_bps": "15",
            },
        )
        assert r_sim.status_code == 200
        assert r_sim.context["permutation"] is not None
        assert r_sim.context["plateau"] is not None
        assert len(r_sim.context["plateau"]["grid"]) == 3
        assert r_sim.context["profit_factor"] is not None
        assert r_sim.context["bar_sharpe"] is not None
        assert r_sim.context["form"]["fee_bps"] == 25.0
        assert r_sim.context["form"]["slippage_bps"] == 15.0

        r_rules = client.get("/rules")
        assert r_rules.status_code == 200
        assert "drift_map" in r_rules.context
        assert len(r_rules.context["rules"][0]["gates"]) == 4

        r_drift_api = client.get("/api/rules/drift")
        assert r_drift_api.status_code == 200
        assert r_drift_api.json()["ok"] is True


def test_trade_prefix_is_invariant_and_experiment_hash_covers_middle_prices():
    bars = _make_bars(110)
    regime = {"stub_idx": _TriggerAtIndexStrategy({66, 107})}
    settings = dict(min_bars=60, max_hold=3, registry_override=regime)
    prefix = backtest.run(bars[:95], "stub_idx", **settings)
    full = backtest.run(bars, "stub_idx", **settings)
    assert prefix["trades"] and full["trades"][:len(prefix["trades"])] == prefix["trades"]
    assert full["bar_returns"][:95-60] == prefix["bar_returns"]
    altered = [dict(row) for row in bars]
    altered[10]["volume"] += 1
    rerun = backtest.run(altered, "stub_idx", **settings)
    assert rerun["stats"]["determinism_hash"] != full["stats"]["determinism_hash"]
