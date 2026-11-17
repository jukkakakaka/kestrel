"""AI 分析引擎（大模型 / WorkBuddy）：模型负责选股和论证，本地强控风控、仓位与 Tick。"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from kestrel.core.money import currency_for, from_cny, round_tick, to_cny, to_micro
from kestrel.core.quantity import db_value, display, tradable_buy_qty
from kestrel.db.engine import connect
from kestrel.fees.calculator import round_trip
from kestrel.indicators.engine import Series, compute
from kestrel.planning.builder import valid_until
from kestrel.receipts import tokens
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import portfolio as portfolio_service
from kestrel.services import rules as rule_service

log = logging.getLogger(__name__)

MIN_STOP_PCT = 0.015
MAX_STOP_PCT = 0.15
MIN_R = 1.5
MAX_PICKS = 6

SYSTEM = """你是一名谨慎的股票交易分析员。你只做多，不使用杠杆，接受"今天不交易"也是正确答案。

你的输出会被程序逐项校验，不合规的建议会被直接丢弃并记录原因。所以：
- 只能从给定的候选列表里选，code 必须原样照抄
- 止损必须落在 brief.constraints 规定的止损带内（min_stop_pct ~ max_stop_pct）
- 目标价至少是入场到止损距离的 1.5 倍
- 不要给仓位建议，仓位与可用现金约束由程序本地强制计算

判断依据要具体，引用你看到的数字（涨幅、RSI、距离均线、量比、VCP 收敛度），
不要写"看好后市"这种没有信息量的话。最多选 3 只，没有合适的就一只都不选。"""

OUTPUT_SPEC = """只输出 JSON，不要 Markdown 代码块，结构如下：

{
  "market_view": "对当前这段行情的一句话判断",
  "picks": [
    {
      "market": "US",
      "code": "AAPL",
      "action": "buy" | "watch",
      "conviction": 0-100 的整数,
      "thesis": "为什么是它，必须引用具体数字，60 字以内",
      "catalysts": ["具体触发因素"],
      "risks": ["什么情况下这个判断会错"],
      "entry_low": 数字,
      "entry_high": 数字,
      "stop": 数字,
      "target": 数字,
      "invalidation": "一句话：看到什么就放弃"
    }
  ],
  "pass_reason": "如果 picks 为空，说明为什么现在不该动"
}"""


@dataclass(slots=True)
class Pick:
    market: str
    code: str
    name: str
    action: str
    conviction: int
    thesis: str
    entry_low: float
    entry_high: float
    stop: float
    target: float
    invalidation: str
    catalysts: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


def build_brief(markets: list[str], limit: int = 40, history: int | None = None) -> dict:
    """组装供大模型推理的盘面简报（与当前启用市场、周期与动态账本严格同步）。"""
    from kestrel.services import advisor, timeframe

    clean_markets = [str(m).upper() for m in (markets or ["CN", "HK", "US"]) if str(m).strip()]
    if not clean_markets:
        clean_markets = ["CN", "HK", "US"]

    conn = connect()
    try:
        settings = config_service.all_values(conn)
        snap = portfolio_service.snapshot(conn=conn, include_quotes=False)
        horizon_code, advice = advisor.resolve(settings.get("trade.horizon"))
        cfg = timeframe.get(horizon_code)
        history = history or cfg.lookback
        marks = ",".join("?" * len(clean_markets))
        rows = conn.execute(
            f"""
            SELECT s.id, s.market, s.code, s.name, s.sector, s.lot_size, s.pool_rank
            FROM symbol s
            LEFT JOIN watchlist w ON w.symbol_id = s.id
            WHERE s.market IN ({marks})
              AND (s.is_pool = 1 OR w.id IS NOT NULL)
            ORDER BY (w.id IS NULL), s.pool_rank
            LIMIT ?
            """,
            (*clean_markets, limit),
        ).fetchall()

        candidates = []
        for row in rows:
            bars = timeframe.prepare(
                bars_service.drop_forming(
                    bars_service.cached_bars(conn, row["id"], history), row["market"]
                ),
                cfg.code,
            )
            if len(bars) < cfg.min_bars:
                continue
            ind = compute(Series.from_bars(bars))
            if not ind:
                continue
            last = float(ind.get("last") or 0)
            if last <= 0:
                continue
            candidates.append(
                {
                    "market": row["market"],
                    "code": row["code"],
                    "name": row["name"],
                    "sector": row["sector"] or "",
                    "price": round(last, 3),
                    "last_date": str(bars[-1]["date"]),
                    "ret5": _round(ind.get("ret5")),
                    "ret20": _round(ind.get("ret20")),
                    "ret60": _round(ind.get("ret60")),
                    "rsi14": _round(ind.get("rsi14")),
                    "atr_pct": _round(ind.get("atr_pct")),
                    "vcp_ratio": _round(ind.get("vcp_ratio")),
                    "vol_ratio": _round(ind.get("vol_ratio")),
                    "vol_today_ratio": _round(ind.get("vol_today_ratio")),
                    "dist_sma20": _round(ind.get("dist_sma20")),
                    "dist_sma60": _round(ind.get("dist_sma60")),
                    "range_pos20": _round(ind.get("range_pos20")),
                    "close_pos": _round(ind.get("close_pos")),
                    "ma_stack": bool(ind.get("ma_stack")),
                    "high20": _round(ind.get("high20")),
                    "low20": _round(ind.get("low20")),
                    "high60": _round(ind.get("high60")),
                    "bars": ind.get("bars"),
                }
            )

        lots = {
            f"{r['market']}:{r['code']}": r["lot_size"]
            for r in conn.execute("SELECT market, code, lot_size FROM symbol").fetchall()
        }
        positions = [
            dict(r)
            for r in conn.execute(
                "SELECT market, code, qty, entry_price, stop_price FROM trade"
                " WHERE status='open'"
            ).fetchall()
        ]
    finally:
        conn.close()

    evidence = []
    for item in rule_service.all_rules():
        if item.get("validation"):
            v = item["validation"]
            evidence.append(
                {
                    "rule": item["name"],
                    "timeframe": v.get("timeframe") or "short",
                    "verdict": v.get("verdict"),
                    "expectancy_r": round(float(v.get("avg_exp") or 0), 3),
                    "edge_over_holding": round(float(v.get("avg_edge") or 0), 3),
                }
            )

    capital_cny = float(snap["total_equity_cny"] or settings.get("account.capital") or 50000)

    return {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "horizon": {
            "code": cfg.code,
            "label": cfg.zh,
            "bar": cfg.bar,
            "max_hold_bars": cfg.max_hold,
            "note": (
                f"价格与指标都基于 {cfg.bar} 线；计划应在 {cfg.max_hold} 根 {cfg.bar} 线内了结。"
                if cfg.code not in ("short", "fast")
                else f"日线级别（{cfg.zh}）；持有约 {cfg.max_hold} 个交易日。"
            ),
        },
        "market_regimes": ((advice or {}).get("metrics") or {}).get("market_regimes") or {},
        "account": {
            "capital_cny": round(capital_cny, 2),
            "available_cash_cny": round(float(snap["available_cash_cny"]), 2),
            "invested_cost_cny": round(float(snap["invested_cost_cny"]), 2),
            "risk_per_trade_pct": settings.get("risk.per_trade_pct"),
            "max_total_exposure_pct": settings.get("risk.max_total_exposure_pct", 60),
            "max_per_sector": settings.get("risk.max_per_sector"),
            "brokers": {
                market: settings.get(f"broker.{market}")
                for market in ("CN", "HK", "US")
            },
        },
        "open_positions": positions,
        "_lots": lots,
        "candidates": candidates,
        "rule_evidence": evidence,
        "constraints": {
            "long_only": True,
            "min_stop_pct": cfg.stop_band[0] * 100,
            "max_stop_pct": cfg.stop_band[1] * 100,
            "min_reward_risk": MIN_R,
            "max_picks": MAX_PICKS,
        },
    }


def _round(value, digits: int = 2):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return round(float(value), digits)
    return None


def render_prompt(brief: dict, locale: str = "zh-CN") -> str:
    return (
        OUTPUT_SPEC
        + "\n\n候选与账户信息如下：\n"
        + json.dumps(brief, ensure_ascii=False)
    )


def budget_state(cfg: dict) -> tuple[bool, int]:
    """检查今日模型调用额度。返回 (是否允许, 已用次数)。"""
    limit = int(cfg.get("agent.daily_budget") or 0)
    if limit <= 0:
        return True, 0
    today = datetime.now(UTC).date().isoformat()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM analyst_run"
            " WHERE provider IN ('llm', 'copilot') AND date(started_at)=? AND error IS NULL",
            (today,),
        ).fetchone()
        used = int(row["n"] or 0)
    finally:
        conn.close()
    return used < limit, used


async def ask_model(brief: dict, cfg: dict, timeout: float = 90.0) -> dict:
    """调用 OpenAI 兼容接口并解析返回 JSON。"""
    base_url = str(cfg.get("agent.base_url") or "").rstrip("/")
    model = str(cfg.get("agent.model") or "")
    api_key = str(cfg.get("agent.api_key") or "")
    if not base_url or not model:
        raise RuntimeError("还没有配置大模型：设置 → 分析引擎，填接口地址和模型名。")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": render_prompt(brief)},
        ],
        "temperature": float(cfg.get("agent.temperature") or 0.2),
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        if resp.status_code >= 300:
            raise RuntimeError(f"模型接口返回 {resp.status_code}：{resp.text[:200]}")
        data = resp.json()

    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return parse_reply(text)


def parse_reply(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("模型没有返回 JSON")
    try:
        return json.loads(cleaned[start : end + 1])
    except ValueError as exc:
        raise RuntimeError(f"模型返回的 JSON 解析失败：{exc}") from exc


def validate_pick(raw: dict, brief: dict) -> tuple[Pick | None, str]:
    """逐项核验大模型选股输出，不合规则直接丢弃并保留原因。"""
    allowed = {(c["market"], c["code"]): c for c in brief["candidates"]}
    market = str(raw.get("market") or "").upper().strip()
    code = str(raw.get("code") or "").strip()
    candidate = allowed.get((market, code))
    if candidate is None:
        return None, f"{market}:{code} 不在候选列表里"

    action = str(raw.get("action") or "").lower()
    if action not in ("buy", "watch"):
        return None, f"动作 {action!r} 不合法"

    price = float(candidate["price"])
    try:
        entry_low = float(raw["entry_low"])
        entry_high = float(raw["entry_high"])
        stop = float(raw["stop"])
        target = float(raw["target"])
    except (KeyError, TypeError, ValueError):
        return None, "价格字段缺失或不是数字"

    for name, value in (
        ("entry_low", entry_low),
        ("entry_high", entry_high),
        ("stop", stop),
        ("target", target),
    ):
        if not (0 < value < price * 5):
            return None, f"{name} 明显不合理（{value}）"

    if entry_low > entry_high:
        return None, "入场区间上下限颠倒"
    if entry_low < price * 0.85 or entry_high > price * 1.15:
        return None, "入场区间偏离现价超过 15%"
    if stop >= entry_low:
        return None, "止损不在入场价下方"

    risk = entry_low - stop
    stop_pct = risk / entry_low
    limits = brief.get("constraints") or {}
    min_stop = float(limits.get("min_stop_pct", MIN_STOP_PCT * 100)) / 100
    max_stop = float(limits.get("max_stop_pct", MAX_STOP_PCT * 100)) / 100
    if not (min_stop <= stop_pct <= max_stop):
        return None, f"止损距离 {stop_pct * 100:.1f}% 超出 {min_stop*100:.0f}%–{max_stop*100:.0f}%"
    if target <= entry_low:
        return None, "目标价不高于入场价"
    if (target - entry_low) / risk < MIN_R:
        return None, f"盈亏比不足 {MIN_R}:1"

    thesis = str(raw.get("thesis") or "").strip()
    if len(thesis) < 8:
        return None, "理由太短，没有信息量"

    try:
        conviction = int(float(raw.get("conviction") or 0))
    except (TypeError, ValueError):
        conviction = 0

    return (
        Pick(
            market=market,
            code=code,
            name=str(candidate.get("name") or code),
            action=action,
            conviction=max(0, min(100, conviction)),
            thesis=thesis[:300],
            entry_low=float(round_tick(entry_low, market, code)),
            entry_high=float(round_tick(entry_high, market, code)),
            stop=float(round_tick(stop, market, code)),
            target=float(round_tick(target, market, code)),
            invalidation=str(raw.get("invalidation") or "")[:200],
            catalysts=[str(c)[:120] for c in (raw.get("catalysts") or [])][:4],
            risks=[str(c)[:120] for c in (raw.get("risks") or [])][:4],
        ),
        "",
    )


def size_position(
    pick: Pick,
    brief: dict,
    lot_size: int,
    broker: str | None = None,
    available_cash_cny: float | None = None,
) -> tuple[Decimal, float, float]:
    """根据风控预算、券商碎股能力及可用现金算可买股数，绝不采用模型给出的数量。"""
    acct = brief.get("account") or {}
    capital = Decimal(str(acct.get("capital_cny") or 50000))
    risk_pct = Decimal(str(acct.get("risk_per_trade_pct") or 2.0))
    cur = currency_for(pick.market)
    capital_local = from_cny(capital, cur)

    entry_d = Decimal(str(pick.entry_low))
    stop_d = Decimal(str(pick.stop))
    risk_budget = capital_local * risk_pct / Decimal("100")
    risk_per_share = entry_d - stop_d
    if risk_per_share <= 0:
        return Decimal("0"), 0.0, 0.0

    lot = max(1, int(lot_size or 1))
    qty = tradable_buy_qty(
        risk_budget / risk_per_share,
        pick.market,
        lot,
        broker,
        code=pick.code,
        entry_price=entry_d,
    )
    cap_qty = tradable_buy_qty(
        (capital_local * Decimal("0.35")) / entry_d,
        pick.market,
        lot,
        broker,
        code=pick.code,
        entry_price=entry_d,
    )
    qty = min(qty, cap_qty)

    if available_cash_cny is not None:
        cash_local = from_cny(Decimal(str(max(0.0, available_cash_cny))), cur)
        cash_qty = tradable_buy_qty(
            (cash_local * Decimal("0.992")) / entry_d,
            pick.market,
            lot,
            broker,
            code=pick.code,
            entry_price=entry_d,
        )
        qty = min(qty, cash_qty)

    if qty <= 0:
        return Decimal("0"), 0.0, 0.0

    cost = entry_d * qty
    fees = round_trip(pick.market, cost, qty, broker)
    return qty, float(fees), float(risk_per_share * qty)


async def analyse(
    markets: list[str] | None = None,
    limit: int = 40,
    dry_reply: dict | None = None,
) -> dict:
    """组装简报 -> 调用模型 -> 校验风控并核算仓位 -> 写入建议与回执 token。"""
    started = datetime.now(UTC)
    conn = connect()
    try:
        cfg = config_service.all_values(conn)
        markets = markets or cfg.get("scan.markets") or ["CN", "HK", "US"]
    finally:
        conn.close()

    brief = build_brief(markets, limit=limit)
    if not brief["candidates"]:
        return {"ok": False, "error": "没有可用的候选股，先跑一次扫描缓存日线。"}

    provider = "dry" if dry_reply is not None else str(cfg.get("agent.provider") or "rules")
    model = str(cfg.get("agent.model") or "")

    if provider == "llm" and dry_reply is None:
        allowed, spent = budget_state(cfg)
        if not allowed:
            _record_run(provider, model, brief, 0, 0, [], "每日调用次数已达上限", None, started)
            return {
                "ok": False,
                "engine": provider,
                "error": (
                    f"今天的模型调用次数已用完（{spent}/{cfg.get('agent.daily_budget')}）。"
                    "可以调高上限、换规则引擎，或者明天再来。"
                ),
                "budget": {"spent": spent, "limit": cfg.get("agent.daily_budget")},
            }

    if provider == "workbuddy" and dry_reply is None:
        from kestrel.services import bridge

        request_id = bridge.queue(
            "analyse",
            SYSTEM + "\n\n" + OUTPUT_SPEC,
            {"brief": brief},
            meta={"provider": "workbuddy"},
        )
        _record_run(provider, "workbuddy", brief, 0, 0, [], None, {"queued": request_id}, started)
        return {
            "ok": True,
            "pending": True,
            "request_id": request_id,
            "provider": provider,
            "candidates": len(brief["candidates"]),
            "picks": 0,
            "accepted": 0,
            "rejected": [],
            "message": (
                "任务已经放进队列，等 WorkBuddy 处理。"
                f" 文件在 {bridge.bridge_info()['pending_dir']}，"
                f"处理完写到 {bridge.bridge_info()['done_dir']}。"
            ),
            "bridge": bridge.bridge_info(),
        }

    try:
        raw_reply = dry_reply if dry_reply is not None else await ask_model(brief, cfg)
    except Exception as exc:  # noqa: BLE001
        _record_run(provider, model, brief, 0, 0, [], str(exc), None, started)
        return {"ok": False, "error": str(exc)}

    return _finish(brief, raw_reply, provider, model, started)


def _finish(brief: dict, raw_reply: dict, provider: str, model: str, started) -> dict:
    """通用校验、仓位与敞口核验、入库。"""
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_sector: dict[str, int] = {}
    acct = brief.get("account") or {}
    max_sector = int(acct.get("max_per_sector") or 2)
    capital = Decimal(str(acct.get("capital_cny") or 50000))
    exposure_pct = Decimal(str(acct.get("max_total_exposure_pct") or 60))
    exposure_cap = capital * exposure_pct / Decimal("100")
    exposure_used = Decimal(str(acct.get("invested_cost_cny") or 0))
    exposure_added = Decimal("0")
    cash_left = float(acct["available_cash_cny"]) if "available_cash_cny" in acct else None

    picks = raw_reply.get("picks") or []
    if not isinstance(picks, list):
        picks = []

    lots = brief.get("_lots") or {}
    brokers = acct.get("brokers") or {}

    for raw in picks[: MAX_PICKS * 2]:
        if not isinstance(raw, dict):
            continue
        pick, reason = validate_pick(raw, brief)
        if pick is None:
            rejected.append({"code": raw.get("code"), "reason": reason})
            continue
        sector = ""
        for c in brief["candidates"]:
            if c["market"] == pick.market and c["code"] == pick.code:
                sector = c.get("sector") or ""
                break
        if sector and seen_sector.get(sector, 0) >= max_sector:
            rejected.append({"code": pick.code, "reason": f"行业集中度：{sector} 已满"})
            continue

        lot = lots.get(f"{pick.market}:{pick.code}", 1)
        broker = brokers.get(pick.market)
        qty, fees, risk_amount = size_position(
            pick, brief, lot, broker, available_cash_cny=cash_left
        )
        if qty <= 0:
            rejected.append(
                {"code": pick.code, "reason": "可用现金或风险预算不足以买最小可成交单位"}
            )
            continue

        cost_cny = to_cny(Decimal(str(pick.entry_low)) * qty, currency_for(pick.market))
        if exposure_cap > 0 and exposure_used + exposure_added + cost_cny > exposure_cap:
            rejected.append({"code": pick.code, "reason": "总仓位敞口已达上限"})
            continue

        exposure_added += cost_cny
        if cash_left is not None:
            cash_left = max(0.0, cash_left - float(cost_cny))
        if sector:
            seen_sector[sector] = seen_sector.get(sector, 0) + 1
        accepted.append(
            {
                "pick": pick,
                "sector": sector,
                "qty": qty,
                "fees": fees,
                "risk_amount": risk_amount,
                "lot": lot,
            }
        )
        if len(accepted) >= MAX_PICKS:
            break

    stored = 0
    if accepted:
        stored = _store(accepted, brief, model or provider)

    duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
    _record_run(provider, model, brief, len(picks), stored, rejected, None, raw_reply, started, duration)

    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "candidates": len(brief["candidates"]),
        "picks": len(picks),
        "accepted": stored,
        "rejected": rejected,
        "market_view": str(raw_reply.get("market_view") or ""),
        "pass_reason": str(raw_reply.get("pass_reason") or ""),
        "duration_ms": duration,
    }


def ingest() -> dict:
    from kestrel.services import bridge

    collected = bridge.collect("analyse")
    if not collected:
        return {"ok": True, "collected": 0, "stored": 0, "results": []}

    results = []
    stored = 0
    for request_id, body in collected:
        brief = ((body.get("_request") or {}).get("input") or {}).get("brief")
        if not brief:
            results.append({"id": request_id, "ok": False, "error": "请求里没有 brief"})
            continue
        created = (body.get("_request") or {}).get("created_at")
        try:
            started = (
                datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if created
                else datetime.now(UTC)
            )
        except ValueError:
            started = datetime.now(UTC)
        try:
            outcome = _finish(brief, body, "workbuddy", "workbuddy", started)
            stored += outcome.get("accepted", 0)
            results.append(
                {
                    "id": request_id,
                    "ok": True,
                    "accepted": outcome.get("accepted", 0),
                    "rejected": outcome.get("rejected", []),
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest %s failed: %s", request_id, exc)
            results.append({"id": request_id, "ok": False, "error": str(exc)})
    return {"ok": True, "collected": len(collected), "stored": stored, "results": results}


def _store(accepted: list[dict], brief: dict, label: str) -> int:
    conn = connect()
    saved = 0
    horizon_code = str((brief.get("horizon") or {}).get("code") or "short")
    try:
        for item in accepted:
            pick: Pick = item["pick"]
            qty: Decimal = item["qty"]
            fees_d = Decimal(str(item["fees"]))
            risk_d = Decimal(str(item["risk_amount"]))
            lot = int(item.get("lot") or 1)
            qty_db = db_value(qty)

            sym_row = conn.execute(
                "SELECT id FROM symbol WHERE market=? AND code=?", (pick.market, pick.code)
            ).fetchone()
            if sym_row is None:
                continue
            symbol_id = sym_row["id"]

            # 作废同标的旧有效建议，确保同一时间只保留最新一条操作计划
            conn.execute(
                "UPDATE recommendation SET status='superseded'"
                " WHERE symbol_id=? AND status='active'",
                (symbol_id,),
            )

            entry_low_d = Decimal(str(pick.entry_low))
            stop_d = Decimal(str(pick.stop))
            target_d = Decimal(str(pick.target))
            r_multiple = float((target_d - entry_low_d) / (entry_low_d - stop_d))
            currency = currency_for(pick.market)

            # 修复致命 Decimal/float 混算 TypeError：全部用 Decimal 计算保本价
            fee_per_share = (fees_d / qty) if qty > 0 else Decimal("0")
            breakeven_d = round_tick(entry_low_d + fee_per_share, pick.market, pick.code)
            target2_d = round_tick(
                entry_low_d + (target_d - entry_low_d) * Decimal("1.5"),
                pick.market,
                pick.code,
            )

            learn_zh, learn_en = _learn_text(pick)
            explain_en = (
                f"Why: {pick.thesis}\n"
                f"How: limit buy {display(qty)} shares near {pick.entry_low:.2f}.\n"
                f"If wrong: exit below {pick.stop:.2f}. {pick.invalidation}"
            )
            cur = conn.execute(
                """
                INSERT INTO recommendation
                    (scan_id, symbol_id, market, code, name, currency, strategy, score,
                     entry_low, entry_high, stop, target1, target2, qty, lot_size,
                     risk_amount, est_fees, breakeven, r_multiple, confidence,
                     valid_until, invalidation, explain_zh, explain_en, status,
                     action, headline, learn_zh, learn_en,
                     source, ai_model, ai_thesis, ai_confidence, horizon)
                VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active',
                        ?,?,?,?,'ai',?,?,?,?)
                """,
                (
                    symbol_id, pick.market, pick.code, pick.name, currency,
                    "ai", pick.conviction / 100.0,
                    to_micro(entry_low_d), to_micro(Decimal(str(pick.entry_high))),
                    to_micro(stop_d), to_micro(target_d),
                    to_micro(target2_d),
                    qty_db, lot,
                    to_micro(risk_d), to_micro(fees_d),
                    to_micro(breakeven_d),
                    round(r_multiple, 2), pick.conviction / 100.0,
                    valid_until(pick.market, horizon_code),
                    json.dumps([pick.invalidation or "model_invalidation"]),
                    pick.thesis, explain_en,
                    pick.action,
                    pick.thesis,
                    learn_zh,
                    learn_en,
                    label,
                    pick.thesis,
                    pick.conviction,
                    horizon_code,
                ),
            )
            rec_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO receipt_token (code, rec_id, expires_at)"
                " VALUES (?,?,?)",
                (tokens.make_code(rec_id), rec_id, tokens.expiry()),
            )
            saved += 1
        conn.commit()
    finally:
        conn.close()
    return saved


def _learn_text(pick: Pick) -> tuple[str, str]:
    risk_pct = (pick.entry_low - pick.stop) / pick.entry_low * 100
    catalysts = "；".join(pick.catalysts) if pick.catalysts else "结合趋势与指标结构触发"
    catalysts_en = "; ".join(pick.catalysts) if pick.catalysts else "technical structure and risk budget"
    zh = (
        f"这是什么：AI 选出的一笔做多计划，理由是「{pick.thesis}」。\n"
        f"为什么是它：{catalysts}。\n"
        f"如果错了会怎样：跌破 {pick.stop:.2f}（约 −{risk_pct:.1f}%）就要走，"
        f"{pick.invalidation or '触发条件失效。'}"
    )
    en = (
        f"What this is: an AI long plan — {pick.thesis}.\n"
        f"Why now: {catalysts_en}.\n"
        f"If wrong: exit below {pick.stop:.2f} (about −{risk_pct:.1f}%). "
        f"{pick.invalidation or 'Setup invalidated.'}"
    )
    return zh, en


def _record_run(provider, model, brief, picks, accepted, rejected, error, reply, started, duration=None):
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO analyst_run
                (provider, model, finished_at, symbols, picks, accepted, rejected,
                 brief, raw_reply, error, duration_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                provider,
                model,
                datetime.now(UTC).isoformat(timespec="seconds"),
                len(brief.get("candidates") or []),
                picks,
                accepted,
                json.dumps(rejected, ensure_ascii=False),
                json.dumps(brief, ensure_ascii=False)[:20000],
                json.dumps(reply, ensure_ascii=False)[:20000] if reply else None,
                error,
                duration,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recent_runs(limit: int = 5) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM analyst_run ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            provider = str(item.get("provider") or "")
            model = str(item.get("model") or "")
            if provider == "workbuddy":
                label = "WorkBuddy"
                detail = ""
            elif provider == "llm":
                label = "大模型"
                detail = model or "未记录模型"
            elif provider == "dry":
                label = "演练"
                detail = ""
            else:
                label = provider or "未知引擎"
                detail = model if model and model != provider else ""
            item["engine_label"] = label
            item["engine_detail"] = detail
            item["horizon"] = _horizon_of(item)
            out.append(item)
        return out
    finally:
        conn.close()


def _horizon_of(item: dict) -> str:
    try:
        brief = json.loads(item.get("brief") or "{}")
    except (TypeError, ValueError):
        return ""
    code = (brief.get("horizon") or {}).get("code")
    return str(code or "")
