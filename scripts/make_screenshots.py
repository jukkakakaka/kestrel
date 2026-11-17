"""统一尺寸与真实演示数据截图脚本（1440×900）。

用法：
1. 自动自托管模式（推荐，不污染本机数据）：
       uv run python scripts/make_screenshots.py --self-serve
   若本地 127.0.0.1:8899 未运行，直接执行 `uv run python scripts/make_screenshots.py`
   也会自动创建独立临时 KESTREL_HOME、播种真实演示数据、启动临时 uvicorn 完成全部截图并清理。

2. 连接外部已运行服务：
       KESTREL_URL=http://127.0.0.1:8899 uv run python scripts/make_screenshots.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

# 全局统一宽屏桌面截图尺寸（移除旧版不一致的 SIZES，确保所有 PNG 像素 100% 一致）
WINDOW_SIZE: tuple[int, int] = (1440, 900)

ONBOARDED_PAGES: dict[str, str] = {
    "today": "/",
    "advice": "/advice",
    "journal": "/journal",
    "market": "/market",
    "rules": "/rules",
}


def _find_chrome() -> str:
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin and Path(env_bin).exists():
        return env_bin
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    raise FileNotFoundError("未找到 Google Chrome / Chromium 可执行文件")


def _is_alive(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def _seed_pre_onboarding(home: Path) -> None:
    """初始化独立临时库，保持未完成引导态但预设 ¥200,000 演示资金供欢迎页展示。"""
    from kestrel.db.engine import connect
    from kestrel.db.migrate import init_db
    from kestrel.fees import plans as plan_service
    from kestrel.services import config as config_service
    from kestrel.services import glossary as glossary_service
    from kestrel.services import rules as rule_service
    from kestrel.settings import settings

    settings.home = home
    settings.ensure_dirs()
    init_db()
    plan_service.hydrate()
    glossary_service.seed()
    rule_service.ensure_seeded()

    conn = connect()
    try:
        config_service.put(conn, "account.capital", 200000.0)
        config_service.put(conn, "risk.per_trade_pct", 1.5)
        config_service.put(conn, "ui.onboarded", False)
        config_service.put(conn, "ui.learning", False)
        config_service.put(conn, "ui.privacy", False)
        conn.commit()
    finally:
        conn.close()


def _seed_demo_workspace(home: Path) -> None:
    """在临时 KESTREL_HOME 中播种真实金融演示数据（资金、自选行情、持仓台账、建议与规则验证）。"""
    from kestrel.core.money import to_micro
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service
    from kestrel.services import execution, journal
    from kestrel.settings import settings

    settings.home = home
    now = datetime.now(UTC)
    valid_until = (now + timedelta(days=2)).isoformat(timespec="seconds")
    now_iso = now.isoformat(timespec="seconds")

    conn = connect()
    try:
        config_service.put(conn, "ui.onboarded", True)
        config_service.put(conn, "account.capital", 200000.0)
        config_service.put(conn, "risk.per_trade_pct", 1.5)
        config_service.put(conn, "risk.max_total_exposure_pct", 80.0)
        config_service.put(conn, "scan.markets", ["CN", "HK", "US"])
        config_service.put(conn, "ui.markets_configured", True)
        config_service.put(conn, "trade.horizon", "short")
        config_service.put(conn, "scan.auto_enabled", True)
        config_service.put(conn, "scan.interval_minutes", 30)
        config_service.put(conn, "ui.learning", False)
        config_service.put(conn, "ui.glossary", True)
        config_service.put(conn, "ui.privacy", False)
        conn.commit()
    finally:
        conn.close()

    demo_symbols = [
        ("CN", "600519", "贵州茅台", "CNY", 100, "SSE", "消费龙头", 1528.00, 1496.50, 2.10),
        ("CN", "300750", "宁德时代", "CNY", 100, "SZSE", "新能源", 248.60, 242.80, 2.39),
        ("HK", "00700", "腾讯控股", "HKD", 100, "HKEX", "互联网", 426.80, 418.20, 2.06),
        ("HK", "03690", "美团-W", "HKD", 100, "HKEX", "本地生活", 168.40, 171.20, -1.64),
        ("US", "NVDA", "NVIDIA", "USD", 1, "NASDAQ", "Semiconductors", 138.60, 134.80, 2.82),
        ("US", "AAPL", "Apple", "USD", 1, "NASDAQ", "Technology", 228.40, 229.90, -0.65),
    ]

    sym_ids: dict[tuple[str, str], int] = {}
    conn = connect()
    try:
        conn.execute("BEGIN")
        for mkt, code, name, cur, lot, exch, sector, px, prev, chg in demo_symbols:
            conn.execute(
                """
                INSERT INTO symbol (market, code, name, currency, lot_size, exchange, sector,
                                    is_pool, pool_rank, bars_depth, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 10, 70, datetime('now'))
                ON CONFLICT(market, code) DO UPDATE SET
                    name=excluded.name, currency=excluded.currency, lot_size=excluded.lot_size,
                    exchange=excluded.exchange, sector=excluded.sector, is_pool=1, bars_depth=70
                """,
                (mkt, code, name, cur, lot, exch, sector),
            )
            sid = int(
                conn.execute(
                    "SELECT id FROM symbol WHERE market=? AND code=?", (mkt, code)
                ).fetchone()["id"]
            )
            sym_ids[(mkt, code)] = sid
            conn.execute("INSERT OR IGNORE INTO watchlist (symbol_id) VALUES (?)", (sid,))
            conn.execute(
                """
                INSERT INTO quote_latest
                    (symbol_id, price, prev_close, open, high, low, volume, change_pct, source, quoted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'eastmoney', ?)
                ON CONFLICT(symbol_id) DO UPDATE SET
                    price=excluded.price, prev_close=excluded.prev_close,
                    change_pct=excluded.change_pct, quoted_at=excluded.quoted_at
                """,
                (
                    sid,
                    to_micro(px),
                    to_micro(prev),
                    to_micro(round(prev * 1.002, 2)),
                    to_micro(round(max(px, prev) * 1.012, 2)),
                    to_micro(round(min(px, prev) * 0.991, 2)),
                    18_500_000,
                    chg,
                    now_iso,
                ),
            )
            # 生成 65 根连贯日 K 线供指标与图表计算
            base_px = px * 0.88
            for idx in range(65):
                day = (now - timedelta(days=90 - idx)).strftime("%Y-%m-%d")
                drift = (px - base_px) * (idx / 64.0)
                wave = ((idx % 7) - 3) * (px * 0.006)
                close_v = round(base_px + drift + wave, 2)
                open_v = round(close_v * (0.994 if idx % 2 == 0 else 1.004), 2)
                high_v = round(max(open_v, close_v) * 1.012, 2)
                low_v = round(min(open_v, close_v) * 0.989, 2)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO bar_daily
                        (symbol_id, trade_date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sid,
                        day,
                        to_micro(open_v),
                        to_micro(high_v),
                        to_micro(low_v),
                        to_micro(close_v),
                        12_000_000 + idx * 150_000,
                    ),
                )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # 播种 2 笔历史已平仓交易（一盈一亏，总体净盈利）
    t_win = execution.record(
        execution.Fill(
            market="US",
            code="AAPL",
            name="Apple",
            side="buy",
            qty=Decimal("30"),
            price=Decimal("212.00"),
            fees=Decimal("2.50"),
            stop=Decimal("202.00"),
            target=Decimal("234.00"),
            filled_at=(now - timedelta(days=24)).strftime("%Y-%m-%d"),
            note="20日突破顺势建仓",
        )
    )
    if t_win.get("trade_id"):
        journal.close_trade(
            int(t_win["trade_id"]),
            "231.50",
            review="到达第一目标位分批止盈",
            rule_break=False,
            exit_reason="take_profit",
        )

    t_loss = execution.record(
        execution.Fill(
            market="HK",
            code="03690",
            name="美团-W",
            side="buy",
            qty=Decimal("200"),
            price=Decimal("176.00"),
            fees=Decimal("48.00"),
            stop=Decimal("169.50"),
            target=Decimal("194.00"),
            filled_at=(now - timedelta(days=16)).strftime("%Y-%m-%d"),
            note="回踩均线试仓",
        )
    )
    if t_loss.get("trade_id"):
        journal.close_trade(
            int(t_loss["trade_id"]),
            "169.40",
            review="严格按计划触及止损离场，亏损控制在 0.8R 内",
            rule_break=False,
            exit_reason="stop_loss",
        )

    # 播种 3 笔真实在持仓位（含 A 股 / 港股 / 美股）
    execution.record(
        execution.Fill(
            market="HK",
            code="00700",
            name="腾讯控股",
            side="buy",
            qty=Decimal("100"),
            price=Decimal("398.00"),
            fees=Decimal("42.50"),
            stop=Decimal("378.00"),
            target=Decimal("452.00"),
            filled_at=(now - timedelta(days=9)).strftime("%Y-%m-%d"),
            note="趋势回踩 20 日均线缩量企稳",
        )
    )
    execution.record(
        execution.Fill(
            market="US",
            code="NVDA",
            name="NVIDIA",
            side="buy",
            qty=Decimal("20"),
            price=Decimal("126.50"),
            fees=Decimal("1.99"),
            stop=Decimal("118.00"),
            target=Decimal("152.00"),
            filled_at=(now - timedelta(days=6)).strftime("%Y-%m-%d"),
            note="放量突破前高平台",
        )
    )
    execution.record(
        execution.Fill(
            market="CN",
            code="300750",
            name="宁德时代",
            side="buy",
            qty=Decimal("100"),
            price=Decimal("239.00"),
            fees=Decimal("5.00"),
            stop=Decimal("226.00"),
            target=Decimal("268.00"),
            filled_at=(now - timedelta(days=3)).strftime("%Y-%m-%d"),
            note="日线多头排列回踩确认",
        )
    )

    # 播种 3 条带中英文详细理由的可执行操作建议
    plans_seed = [
        {
            "mkt": "CN",
            "code": "600519",
            "name": "贵州茅台",
            "cur": "CNY",
            "strategy": "trend_pullback",
            "score": 0.82,
            "low": 1516.00,
            "high": 1530.00,
            "stop": 1468.00,
            "target": 1645.00,
            "qty": 100,
            "lot": 100,
            "risk": 2950.00,
            "fees": 192.00,
            "be": 1524.92,
            "rr": 2.4,
            "zh": "股价稳居 MA20/MA60 多头均线之上，近三日缩量回踩 20 日线支撑且 RSI(14) 处于 54 健康区间；建议在限价区间内挂单，跌破 1468 严格止损。",
            "en": "Price holds firmly above rising MA20/MA60 with contracting pullback volume and neutral RSI(14) at 54. Enter via limit order inside the band; hard stop at ¥1,468.00.",
        },
        {
            "mkt": "US",
            "code": "AAPL",
            "name": "Apple",
            "cur": "USD",
            "strategy": "breakout_20",
            "score": 0.76,
            "low": 227.20,
            "high": 229.50,
            "stop": 218.50,
            "target": 249.80,
            "qty": 35,
            "lot": 1,
            "risk": 343.00,
            "fees": 2.40,
            "be": 228.47,
            "rr": 2.3,
            "zh": "日线收敛整理后贴近 20 日高点蓄势，ATR 波动率收敛至 1.6%，盈亏比达 2.3:1，按账户 1.5% 风险预算匹配 35 股。",
            "en": "Tight consolidation beneath 20-session high with ATR compressing to 1.6%. Offers 2.3:1 reward-to-risk; sized to 35 shares within your 1.5% risk cap.",
        },
        {
            "mkt": "HK",
            "code": "03690",
            "name": "美团-W",
            "cur": "HKD",
            "strategy": "pullback_quiet",
            "score": 0.71,
            "low": 166.80,
            "high": 169.20,
            "stop": 157.50,
            "target": 191.00,
            "qty": 200,
            "lot": 100,
            "risk": 2100.00,
            "fees": 86.00,
            "be": 168.43,
            "rr": 2.2,
            "zh": "中期上升通道内缩量回撤至前高颈线位，含双边港股印花税与平台费保本价为 HK$168.43，建议分批限价低吸。",
            "en": "Low-volume pullback to prior breakout neckline inside an intermediate uptrend. Round-trip HK stamp duty included in HK$168.43 break-even.",
        },
    ]

    conn = connect()
    try:
        conn.execute("BEGIN")
        cur_scan = conn.execute(
            """
            INSERT INTO scan_run (started_at, finished_at, trigger, markets, scanned, signals, plans, status, horizon)
            VALUES (?, ?, 'manual', '["CN","HK","US"]', 90, 7, 3, 'ok', 'short')
            """,
            (now_iso, now_iso),
        )
        scan_id = int(cur_scan.lastrowid)
        for item in plans_seed:
            sid = sym_ids[(item["mkt"], item["code"])]
            conn.execute(
                """
                INSERT INTO recommendation
                    (scan_id, symbol_id, market, code, name, currency, strategy, horizon,
                     action, score, entry_low, entry_high, stop, target1, qty, lot_size,
                     risk_amount, est_fees, breakeven, r_multiple, confidence,
                     valid_until, invalidation, headline, explain_zh, explain_en, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'short', 'buy', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.8,
                        ?, '["close_below_stop"]', ?, ?, ?, 'active')
                """,
                (
                    scan_id,
                    sid,
                    item["mkt"],
                    item["code"],
                    item["name"],
                    item["cur"],
                    item["strategy"],
                    item["score"],
                    to_micro(item["low"]),
                    to_micro(item["high"]),
                    to_micro(item["stop"]),
                    to_micro(item["target"]),
                    item["qty"],
                    item["lot"],
                    to_micro(item["risk"]),
                    to_micro(item["fees"]),
                    to_micro(item["be"]),
                    item["rr"],
                    valid_until,
                    "",
                    item["zh"],
                    item["en"],
                ),
            )

        # 为内置规则填充三折滚动验证与固定种子置换检验 p 值，使规则页四关证伪完整点亮
        rule_metrics = [
            ("trend_pullback", "有优势", 0.34, 0.21, 0.008, [
                {"fold": 1, "n": 42, "win": 57.1, "exp": 0.36, "bench": 0.14, "edge": 0.22, "pf": 1.84, "dd": 3.2, "streak": 3},
                {"fold": 2, "n": 38, "win": 52.6, "exp": 0.29, "bench": 0.11, "edge": 0.18, "pf": 1.65, "dd": 4.1, "streak": 4},
                {"fold": 3, "n": 45, "win": 57.8, "exp": 0.38, "bench": 0.15, "edge": 0.23, "pf": 1.92, "dd": 2.9, "streak": 3},
            ]),
            ("breakout_20", "有优势", 0.28, 0.16, 0.021, [
                {"fold": 1, "n": 31, "win": 48.4, "exp": 0.31, "bench": 0.13, "edge": 0.18, "pf": 1.71, "dd": 4.4, "streak": 4},
                {"fold": 2, "n": 35, "win": 45.7, "exp": 0.24, "bench": 0.10, "edge": 0.14, "pf": 1.54, "dd": 4.8, "streak": 5},
                {"fold": 3, "n": 33, "win": 51.5, "exp": 0.30, "bench": 0.14, "edge": 0.16, "pf": 1.69, "dd": 3.7, "streak": 4},
            ]),
            ("pullback_quiet", "有优势", 0.31, 0.19, 0.014, [
                {"fold": 1, "n": 29, "win": 55.2, "exp": 0.33, "bench": 0.12, "edge": 0.21, "pf": 1.79, "dd": 3.1, "streak": 3},
                {"fold": 2, "n": 32, "win": 53.1, "exp": 0.28, "bench": 0.11, "edge": 0.17, "pf": 1.62, "dd": 3.6, "streak": 4},
                {"fold": 3, "n": 27, "win": 55.6, "exp": 0.32, "bench": 0.13, "edge": 0.19, "pf": 1.76, "dd": 3.0, "streak": 3},
            ]),
        ]
        for r_code, verdict, avg_exp, avg_edge, p_val, folds in rule_metrics:
            conn.execute(
                "UPDATE strategy_rule SET last_verdict=?, last_validated=? WHERE code=?",
                (verdict, now_iso, r_code),
            )
            payload = {"folds": folds, "p_value": p_val}
            conn.execute(
                """
                INSERT INTO rule_validation (code, timeframe, verdict, avg_exp, avg_edge, folds, symbols, created_at)
                VALUES (?, 'short', ?, ?, ?, ?, 90, ?)
                """,
                (r_code, verdict, avg_exp, avg_edge, json.dumps(payload), now_iso),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO rule_timeframe_state (code, timeframe, verdict, avg_edge, symbols, validated_at)
                VALUES (?, 'short', ?, ?, 90, ?)
                """,
                (r_code, verdict, avg_edge, now_iso),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def capture(
    chrome_bin: str,
    url: str,
    locale: str,
    target: Path,
    size: tuple[int, int] = WINDOW_SIZE,
    theme: str = "light",
) -> bool:
    """使用独立无痕 Profile 截取固定 1440×900 视口截图，显式传入语言与主题参数。"""
    profile = tempfile.mkdtemp(prefix="kestrel-shot-")
    q_locale = "zh-CN" if locale == "zh" else locale
    base_part, frag_part = (url.split("#", 1) + [""])[:2]
    sep = "&" if "?" in base_part else "?"
    final = f"{base_part}{sep}locale={q_locale}&theme={theme}" + (f"#{frag_part}" if frag_part else "")
    tmp_shot = Path(profile) / "shot.png"

    args = [
        chrome_bin,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}",
        "--force-device-scale-factor=1",
        f"--force-prefers-color-scheme={'dark' if theme == 'dark' else 'light'}",
        "--virtual-time-budget=3500",
        f"--screenshot={tmp_shot}",
        final,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and tmp_shot.exists() and tmp_shot.stat().st_size > 1024:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_shot), str(target))
            return True
        if result.stderr:
            print(f"[screenshot failed] {final}: {result.stderr.strip()}")
        return False
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _run_self_serve(chrome_bin: str, theme: str) -> list[str]:
    """在隔离临时 KESTREL_HOME 下自动播种演示数据并启动临时 uvicorn 完成全套中英文截图。"""
    tmp_home = Path(tempfile.mkdtemp(prefix="kestrel-demo-home-"))
    proc: subprocess.Popen[str] | None = None
    failures: list[str] = []
    try:
        _seed_pre_onboarding(tmp_home)
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env["KESTREL_HOME"] = str(tmp_home)
        env["KESTREL_TRAY"] = "0"

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "kestrel.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        ready = False
        for _ in range(50):
            if _is_alive(base):
                ready = True
                break
            time.sleep(0.2)
        if not ready:
            err_out = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"临时 Kestrel 服务启动超时：{err_out}")

        # 1. 截取未引导态欢迎页（中英文）
        for loc in ("zh", "en"):
            target = OUT / f"welcome.{loc}.png"
            ok = capture(chrome_bin, f"{base}/welcome", loc, target, WINDOW_SIZE, theme=theme)
            dims = _png_size(target) if ok else (0, 0)
            print(f"{'ok  ' if ok else 'FAIL'} {target.relative_to(ROOT)} ({dims[0]}x{dims[1]})")
            if not ok:
                failures.append(str(target))

        # 2. 播种完整真实演示数据并截取核心业务工作台（中英文）
        _seed_demo_workspace(tmp_home)
        for name, path in ONBOARDED_PAGES.items():
            for loc in ("zh", "en"):
                target = OUT / f"{name}.{loc}.png"
                ok = capture(chrome_bin, f"{base}{path}", loc, target, WINDOW_SIZE, theme=theme)
                dims = _png_size(target) if ok else (0, 0)
                print(f"{'ok  ' if ok else 'FAIL'} {target.relative_to(ROOT)} ({dims[0]}x{dims[1]})")
                if not ok:
                    failures.append(str(target))
        return failures
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp_home, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成统一 1440x900 分辨率的 Kestrel 中英文截图")
    parser.add_argument("--url", default="", help="指定已运行的 Kestrel 服务地址")
    parser.add_argument(
        "--self-serve",
        action="store_true",
        help="强制使用独立临时 KESTREL_HOME 自动播种真实演示数据并启动临时服务截图",
    )
    parser.add_argument("--theme", choices=("light", "dark"), default="light", help="截图外观主题")
    args = parser.parse_args()

    chrome_bin = _find_chrome()
    OUT.mkdir(parents=True, exist_ok=True)

    explicit_url = (args.url or os.environ.get("KESTREL_URL") or "").rstrip("/")
    default_url = "http://127.0.0.1:8899"

    if args.self_serve or not _is_alive(explicit_url or default_url):
        print(f"[make_screenshots] 启用独立临时演示环境（统一尺寸 {WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}）…")
        failures = _run_self_serve(chrome_bin, theme=args.theme)
    else:
        base = explicit_url or default_url
        print(f"[make_screenshots] 连接外部服务 {base}（统一尺寸 {WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}）…")
        failures = []
        all_pages = {"welcome": "/welcome", **ONBOARDED_PAGES}
        for name, path in all_pages.items():
            for loc in ("zh", "en"):
                target = OUT / f"{name}.{loc}.png"
                ok = capture(chrome_bin, f"{base}{path}", loc, target, WINDOW_SIZE, theme=args.theme)
                dims = _png_size(target) if ok else (0, 0)
                print(f"{'ok  ' if ok else 'FAIL'} {target.relative_to(ROOT)} ({dims[0]}x{dims[1]})")
                if not ok:
                    failures.append(str(target))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
