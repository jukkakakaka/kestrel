"""多市场与交易周期双维智能顾问（Market & Horizon Advisor）。

当用户还没有手动配置市场或周期时，这里给出透明、有依据的默认推荐：
* **市场推荐**：综合账户资金规模、各市场整手/碎股门槛、交易税费磨损与跨市场分散，
  给出推荐市场组合及每个市场的适配理由。
* **周期推荐**：综合行情广度（站上 60 日均线比例）、波动率（ATR% 中位数）、
  当前账户总资产与费率磨损、以及当前敞口水位，评估超短 / 短线 / 中线 / 长线。
  冷启动无 K 线数据时依然根据本金与费率给出合理建议，且不锁死缓存。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kestrel.core.money import currency_for, from_cny
from kestrel.db.engine import connect
from kestrel.fees.calculator import round_trip
from kestrel.indicators.engine import Series, compute
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import portfolio as portfolio_service
from kestrel.services import timeframe

log = logging.getLogger(__name__)

CACHE_KEY = "advisor.horizon"
CACHE_TTL = timedelta(minutes=15)
MIN_SAMPLE = 8


def _reason(
    code: str,
    zh: str,
    en: str,
    weight: float,
    targets: list[str] | None = None,
) -> dict:
    return {
        "code": code,
        "zh": zh,
        "en": en,
        "weight": weight,
        "targets": targets or [],
    }


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _breadth(symbols: list[dict], conn) -> dict:
    """统计指定标的集合中站上 60 日均线比例、ATR% 中位数、20 日收益中位数及分市场环境。"""
    above = total = 0
    atrs: list[float] = []
    rets: list[float] = []
    latest = ""
    per_market_raw: dict[str, dict] = {}

    for sym in symbols:
        rows = bars_service.cached_bars(conn, sym["id"], 200)
        if len(rows) < 80:
            continue
        ind = compute(Series.from_bars(rows))
        if not ind or ind.get("sma60") is None:
            continue
        mkt = str(sym.get("market") or "").upper()
        bucket = per_market_raw.setdefault(
            mkt, {"above": 0, "total": 0, "atrs": [], "rets": []}
        )
        total += 1
        bucket["total"] += 1
        is_above = ind["last"] > ind["sma60"]
        if is_above:
            above += 1
            bucket["above"] += 1
        if ind.get("atr_pct"):
            val = float(ind["atr_pct"])
            atrs.append(val)
            bucket["atrs"].append(val)
        if ind.get("ret20") is not None:
            val = float(ind["ret20"])
            rets.append(val)
            bucket["rets"].append(val)
        latest = max(latest, str(rows[-1]["date"])[:10])

    regimes: dict[str, dict] = {}
    for mkt, b in per_market_raw.items():
        br = (b["above"] / b["total"]) if b["total"] else 0.0
        med_ret = _median(b["rets"])
        med_atr = _median(b["atrs"])
        if b["total"] < 5:
            state = "unknown"
        elif br >= 0.58 and med_ret >= 0:
            state = "bull"
        elif br <= 0.30 or (br < 0.38 and med_ret < -4):
            state = "bear"
        else:
            state = "mixed"
        regimes[mkt] = {
            "sample": b["total"],
            "breadth": round(br, 3),
            "atr_pct": round(med_atr, 2),
            "ret20": round(med_ret, 2),
            "ret_20d": round(med_ret, 2),
            "state": state,
            "regime": state,
            "label_zh": {"bull": "偏多", "bear": "偏弱", "mixed": "震荡", "unknown": "待采样"}[state],
            "label_en": {"bull": "Bullish", "bear": "Weak", "mixed": "Mixed", "unknown": "Unverified"}[state],
        }

    return {
        "sample": total,
        "breadth": (above / total) if total else 0.0,
        "atr_pct": _median(atrs),
        "ret20": _median(rets),
        "bars_as_of": latest,
        "market_regimes": regimes,
    }


def _cost_pct(capital_cny: float, market: str, broker: str | None) -> float:
    """用一笔占总资金 15% 的代表性交易计算单次来回费用占比（%）。"""
    cur = currency_for(market)
    ticket = from_cny(Decimal(str(max(capital_cny, 1))) * Decimal("0.15"), cur)
    if ticket <= 0:
        return 0.0
    qty = max(1, int(ticket / Decimal("100")))
    try:
        fees = round_trip(market.upper(), ticket, Decimal(qty), broker)
    except Exception:  # noqa: BLE001
        return 0.0
    return float(fees / ticket * 100)


def _symbols(conn, markets: list[str] | None = None) -> list[dict]:
    """仅读取当前已启用市场的候选池与自选股，绝不跨市场混算。"""
    clean = [str(m).upper() for m in (markets or ["CN", "HK", "US"]) if str(m).strip()]
    if not clean:
        clean = ["CN", "HK", "US"]
    marks = ",".join("?" * len(clean))
    rows = conn.execute(
        f"""
        SELECT s.id, s.market, s.code, s.name FROM symbol s
        LEFT JOIN watchlist w ON w.symbol_id = s.id
        WHERE s.market IN ({marks}) AND (s.is_pool = 1 OR w.id IS NOT NULL)
        ORDER BY s.is_pool DESC, s.pool_rank LIMIT 240
        """,
        tuple(clean),
    ).fetchall()
    return [dict(r) for r in rows]


def recommend_markets(
    capital_cny: float | None = None,
    *,
    configured: bool | None = None,
    enabled: list[str] | None = None,
    brokers: dict[str, str | None] | None = None,
) -> dict:
    """根据资金规模、整手门槛与税费特征推荐交易市场组合并给出解释。"""
    conn = connect()
    try:
        cfg = config_service.all_values(conn)
        if capital_cny is None:
            snap = portfolio_service.snapshot(conn=conn, include_quotes=False)
            capital_cny = float(snap["total_equity_cny"] or cfg.get("account.capital") or 50000)
        if configured is None:
            configured = bool(cfg.get("ui.markets_configured", False))
        if enabled is None:
            enabled = [str(m).upper() for m in (cfg.get("scan.markets") or ["CN", "HK", "US"])]
        brokers = brokers or {
            mkt: cfg.get(f"broker.{mkt}") for mkt in ("CN", "HK", "US")
        }
    finally:
        conn.close()

    cap = max(100.0, float(capital_cny))
    cost_cn = _cost_pct(cap, "CN", brokers.get("CN"))
    cost_hk = _cost_pct(cap, "HK", brokers.get("HK"))
    cost_us = _cost_pct(cap, "US", brokers.get("US"))

    if cap < 18000:
        recommended = ["US", "CN"] if cap >= 10000 else ["US"]
        primary_zh = (
            f"当前资金约 ¥{cap:,.0f}：优先推荐美股（可买 1 股或小数股、无印花税），"
            + ("搭配 A 股精选低单价标的；港股因每手门槛与最低平台费暂不建议重配。"
               if cap >= 10000 else "避免 A 股/港股一手门槛过高导致频繁被风控拒绝。")
        )
        primary_en = (
            f"With ~¥{cap:,.0f} capital, US stocks fit best (1-share/fractional sizing, zero stamp duty)"
            + (" alongside lower-priced A-shares; HK lot minimums and platform fees are heavy here."
               if cap >= 10000 else " to avoid whole-lot rejections on CN/HK.")
        )
        fit = {
            "US": "high",
            "CN": "medium" if cap >= 10000 else "low",
            "HK": "low",
        }
    elif cap < 150000:
        recommended = ["CN", "US"]
        primary_zh = (
            f"当前资金约 ¥{cap:,.0f}：推荐「A 股 + 美股」双市场组合，兼具本土 T+1 趋势票与美股科技/ETF，单笔风险预算能覆盖多数整手标的且费用合理。"
        )
        primary_en = (
            f"With ~¥{cap:,.0f} capital, CN + US balances onshore liquidity with single-share/ETF flexibility while keeping fees moderate."
        )
        fit = {"CN": "high", "US": "high", "HK": "medium"}
    else:
        recommended = ["CN", "HK", "US"]
        primary_zh = (
            f"当前资金约 ¥{cap:,.0f}：资金体量充足，推荐开启「A 股 + 港股 + 美股」三市场扫描，摊薄港股固定平台费并实现跨市场机会分散。"
        )
        primary_en = (
            f"With ~¥{cap:,.0f} capital, enabling all three markets (CN + HK + US) dilutes HK fixed ticket fees and broadens diversification."
        )
        fit = {"CN": "high", "HK": "high", "US": "high"}

    per_market = {
        "CN": {
            "market": "CN",
            "fit": fit["CN"],
            "recommended": "CN" in recommended,
            "est_cost_pct": round(cost_cn, 3),
            "reason_zh": (
                f"100 股一手（科创板 200 股起），T+1 与涨跌停限制；代表性来回税费约 {cost_cn:.2f}%。"
                + ("当前本金对高价 A 股一手压力偏大。" if cap < 15000 else "流动性充沛、交易成本低，适合主战场。")
            ),
            "reason_en": (
                f"100-share lots (200+ on STAR), T+1 and price limits; ~{cost_cn:.2f}% round-trip fees. "
                + ("Tight on small capital for expensive names." if cap < 15000 else "Low fee drag and deep liquidity.")
            ),
        },
        "HK": {
            "market": "HK",
            "fit": fit["HK"],
            "recommended": "HK" in recommended,
            "est_cost_pct": round(cost_hk, 3),
            "reason_zh": (
                f"每手 100–1000 股不等（部分券商支持碎股），买卖双向印花税 0.1%；代表性来回费率约 {cost_hk:.2f}%。"
                + ("小资金下固定平台费占比较高。" if cap < 50000 else "与 A 股互补性好，适合中大资金配置核心资产。")
            ),
            "reason_en": (
                f"Varying board lots (odd lots on select brokers) and 0.1% two-way stamp duty; ~{cost_hk:.2f}% round-trip. "
                + ("Fixed fees bite small orders." if cap < 50000 else "Complements CN holdings for mid-to-large accounts.")
            ),
        },
        "US": {
            "market": "US",
            "fit": fit["US"],
            "recommended": "US" in recommended,
            "est_cost_pct": round(cost_us, 3),
            "reason_zh": (
                f"可买 1 股（支持小数股 0.0001 股），T+0 且无印花税；代表性来回费用约 {cost_us:.2f}%，对各资金规模仓位控制都最精确。"
            ),
            "reason_en": (
                f"1-share or fractional (0.0001) sizing, T+0 and no stamp duty; ~{cost_us:.2f}% round-trip for exact risk sizing."
            ),
        },
    }

    rec_hor = "medium" if cap < 15000 else ("short" if cap < 150000 else "short")
    try:
        cached_eval = cached()
        if cached_eval and cached_eval.get("horizon") and not cached_eval.get("cold_start"):
            rec_hor = str(cached_eval["horizon"])
    except Exception:  # noqa: BLE001
        pass
    hor_cfg = timeframe.get(rec_hor)
    reasons_zh = [primary_zh] + [f"{m}：{per_market[m]['reason_zh']}" for m in recommended]
    reasons_en = [primary_en] + [f"{m}: {per_market[m]['reason_en']}" for m in recommended]

    return {
        "ok": True,
        "capital_cny": round(cap, 2),
        "configured": bool(configured),
        "enabled": enabled,
        "recommended": recommended,
        "recommended_markets": recommended,
        "recommended_horizon": hor_cfg.code,
        "recommended_horizon_zh": hor_cfg.zh,
        "recommended_horizon_en": hor_cfg.en,
        "primary_zh": primary_zh,
        "primary_en": primary_en,
        "reasons_zh": reasons_zh,
        "reasons_en": reasons_en,
        "reasons": reasons_zh,
        "per_market": per_market,
    }


def evaluate(force: bool = False) -> dict:
    """结合市场广度、波动率、动态总资产与多市场综合费率，评估推荐交易周期。"""
    if not force:
        stored = cached()
        if stored and stored.get("fresh") and not stored.get("cold_start"):
            return stored

    conn = connect()
    try:
        settings_map = config_service.all_values(conn)
        snap = portfolio_service.snapshot(conn=conn, include_quotes=False)
        capital = float(snap["total_equity_cny"] or settings_map.get("account.capital") or 50000)
        markets = [
            str(m).upper()
            for m in (settings_map.get("scan.markets") or ["CN", "HK", "US"])
            if str(m).strip()
        ] or ["CN", "HK", "US"]
        exposure_pct = float(settings_map.get("risk.max_total_exposure_pct", 60) or 60)
        brokers = {mkt: settings_map.get(f"broker.{mkt}") for mkt in markets}
        symbols = _symbols(conn, markets)
        regime = _breadth(symbols, conn)
    finally:
        conn.close()

    scores = {code: 0.0 for code in timeframe.ORDER}
    reasons: list[dict] = []

    def add(codes: dict[str, float], item: dict) -> None:
        positive_targets = [c for c, w in codes.items() if w > 0]
        item["targets"] = positive_targets
        for code, weight in codes.items():
            if code in scores:
                scores[code] += weight
        reasons.append(item)

    breadth = regime["breadth"]
    atr = regime["atr_pct"]
    sample = regime["sample"]
    cold_start = sample < MIN_SAMPLE

    # 1. 不依赖 K 线的资金规模与税费磨损评估（冷启动期也完全生效）
    costs = [_cost_pct(capital, m, brokers.get(m)) for m in markets]
    valid_costs = [c for c in costs if c > 0]
    cost = (sum(valid_costs) / len(valid_costs)) if valid_costs else 0.55

    drags = {
        code: cost * (timeframe.get(code).per_year / max(1, timeframe.get(code).max_hold))
        for code in timeframe.ORDER
    }

    if capital < 25000:
        add(
            {"medium": 1.6, "long": 0.6, "short": -0.4, "fast": -2.0},
            _reason(
                "capital_small",
                f"当前总资产 ¥{capital:,.0f} 偏小，高频短线单笔最低费率占比高，中线周线减少换手更能留住利润。",
                f"Account equity ¥{capital:,.0f} is compact — weekly holding lowers fee drag compared with fast swings.",
                1.6,
            ),
        )
    elif capital > 250000:
        add(
            {"short": 0.8, "fast": 0.5, "medium": 0.5},
            _reason(
                "capital_large",
                f"当前总资产 ¥{capital:,.0f}，单笔固定平台费被充分摊薄，短线与快波段均可灵活运作。",
                f"Equity ¥{capital:,.0f} dilutes fixed ticket fees, supporting active daily/fast swings.",
                0.8,
            ),
        )
    else:
        add(
            {"short": 0.8, "medium": 0.6},
            _reason(
                "capital_mid",
                f"当前总资产 ¥{capital:,.0f}，短线日线与中线周线兼顾机会频率与税费成本。",
                f"Equity ¥{capital:,.0f} balances opportunity count and fee drag on daily/weekly bars.",
                0.8,
            ),
        )

    if drags["short"] >= 8.0 or drags["fast"] >= 18.0:
        add(
            {"fast": -1.5, "short": -1.0, "medium": 0.8},
            _reason(
                "cost_drag",
                f"按所选市场综合费率，超短/短线年化交易磨损分别约 {drags['fast']:.1f}% / {drags['short']:.1f}%，"
                f"放宽至中线可压低到 {drags['medium']:.1f}%。",
                f"Estimated annual fee drag is {drags['fast']:.1f}% (fast) / {drags['short']:.1f}% (daily) vs {drags['medium']:.1f}% on weekly bars.",
                1.2,
            ),
        )

    # 2. 行情广度与波动率评估（需满足样本门槛）
    if cold_start:
        reasons.append(
            _reason(
                "cold_start",
                "本地暂无足够历史 K 线缓存，当前先按账户资金与费率给出周期推荐；首次扫描拉取 K 线后会自动结合市场广度精算。",
                "Not enough cached history yet — sizing recommendation from equity and fees; will refine with market breadth after your first scan.",
                0.5,
            )
        )
    else:
        if breadth >= 0.60:
            add(
                {"medium": 1.5, "long": 1.0, "short": 0.8},
                _reason(
                    "breadth_strong",
                    f"已启用市场 {breadth * 100:.0f}% 标的站在 60 日均线上方，多头趋势面健康，中长线拿得住利润。",
                    f"{breadth * 100:.0f}% of enabled markets sit above their 60-bar average — healthy trend breadth rewards position holding.",
                    1.5,
                ),
            )
        elif breadth >= 0.32:
            add(
                {"short": 0.8, "medium": 0.6},
                _reason(
                    "breadth_mixed",
                    f"{breadth * 100:.0f}% 标的在 60 日线上方，属于结构震荡市，适合日线或周线波段精选个股。",
                    f"{breadth * 100:.0f}% above 60-bar average — mixed tape favours selective daily or weekly swings.",
                    0.8,
                ),
            )
        else:
            add(
                {"short": 0.4, "medium": 0.3, "fast": -1.0, "long": -2.0},
                _reason(
                    "breadth_weak",
                    f"仅 {breadth * 100:.0f}% 标的站在 60 日均线上方，市场偏弱防守为主：严控仓位、不盲目长线死扛，也不高频接飞刀。",
                    f"Only {breadth * 100:.0f}% above 60-bar average — weak tape calls for defence, tight selectivity, and avoiding long holds.",
                    1.0,
                ),
            )

        if atr >= 4.5:
            add(
                {"short": 1.2, "fast": 0.8, "long": -1.2},
                _reason(
                    "vol_high",
                    f"日均波幅中位数 {atr:.1f}%，波动剧烈，应缩短持仓预期并严守止损。",
                    f"Median daily range is {atr:.1f}% — elevated volatility calls for shorter holding windows.",
                    1.2,
                ),
            )
        elif atr and atr <= 2.2:
            add(
                {"medium": 1.0, "long": 1.2, "fast": -0.6},
                _reason(
                    "vol_low",
                    f"日均波幅中位数仅 {atr:.1f}%，低波稳健走势适合放宽周期少折腾。",
                    f"Median daily range is just {atr:.1f}% — calm tape favours patient holding over rapid trading.",
                    1.2,
                ),
            )

    cap_value = Decimal(str(capital)) * Decimal(str(exposure_pct)) / Decimal("100")
    open_value = Decimal(str(snap["market_value_cny"]))
    used_pct = float(open_value / cap_value * 100) if cap_value else 0.0
    blocked = used_pct >= 90
    note = ""
    if blocked:
        note = (
            f"持仓已占用仓位上限的 {used_pct:.0f}%，新一轮扫描里大部分新买入计划会因为敞口上限被拒。"
            "可先处理接近目标或走弱持仓，再扫描开新仓。"
        )

    order = sorted(
        timeframe.ORDER,
        key=lambda code: (-scores[code], timeframe.ORDER.index(code)),
    )
    best = order[0]
    runner = order[1]
    margin = scores[best] - scores[runner]
    confidence = 0.25 if cold_start else max(0.0, min(0.92, 0.35 + margin * 0.15))

    # 只从真正给胜出周期贡献正向分数的理由里挑主理由，防止张冠李戴
    aligned = [r for r in reasons if best in (r.get("targets") or [])]
    primary = (
        max(aligned, key=lambda r: r["weight"])
        if aligned
        else (reasons[0] if reasons else None)
    )

    report = {
        "ok": True,
        "horizon": best,
        "confidence": round(confidence, 2),
        "cold_start": cold_start,
        "scores": {code: round(value, 2) for code, value in scores.items()},
        "reasons": reasons,
        "reason": primary["zh"] if primary else "",
        "primary_reason": primary["zh"] if primary else "",
        "primary_reason_en": primary["en"] if primary else "",
        "market_regimes": regime.get("market_regimes") or {},
        "metrics": regime
        | {
            "capital": round(capital, 2),
            "markets": markets,
            "cost_pct": round(cost, 3),
            "cost_drag": {k: round(v, 2) for k, v in drags.items()},
            "exposure_used_pct": round(used_pct, 1),
            "exposure_cap_pct": exposure_pct,
        },
        "blocked": blocked,
        "note": note,
    }
    if cold_start:
        report["at"] = datetime.now(UTC).isoformat(timespec="seconds")
        report["fresh"] = False
        return report
    return _store(report)


def _store(report: dict) -> dict:
    report["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    report.setdefault("fresh", True)
    payload = {key: value for key, value in report.items() if key != "fresh"}
    conn = connect()
    try:
        config_service.put(conn, CACHE_KEY, payload)
        conn.commit()
    finally:
        conn.close()
    return report


def cached() -> dict | None:
    conn = connect()
    try:
        stored = config_service.get(conn, CACHE_KEY, None)
    finally:
        conn.close()
    if not stored:
        return None
    try:
        moment = datetime.fromisoformat(str(stored.get("at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return stored | {"fresh": False}
    fresh = datetime.now(UTC) - moment < CACHE_TTL
    return stored | {"fresh": fresh}


def recommend(force: bool = False) -> dict:
    """获取当前建议周期：命中有效非冷启动缓存则返回，否则重算。"""
    report = evaluate(force=force)
    if not timeframe.is_valid(report.get("horizon")):
        report["horizon"] = timeframe.DEFAULT
    return report


def resolve(requested: str | None) -> tuple[str, dict | None]:
    """将 ``trade.horizon`` 配置解析为具体执行的周期编码。"""
    code = str(requested or timeframe.AUTO).strip().lower()
    if timeframe.is_valid(code):
        return code, None
    try:
        report = recommend()
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor failed, staying on %s: %s", timeframe.DEFAULT, exc)
        return timeframe.DEFAULT, None
    return report.get("horizon") or timeframe.DEFAULT, report
