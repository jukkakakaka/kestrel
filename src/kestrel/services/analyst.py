"""Let a model do the thinking, and keep the arithmetic local.

The rule engine decides by comparing indicators to numbers. That is auditable but
it cannot weigh a earnings date, a sector rotation or a chart shape it was not
told about. This module hands a model the actual market snapshot and asks it to
pick and argue — then enforces every number it returns.

The division of labour is deliberate:

* the model chooses *what* and *why*: which names, which action, the reasoning,
  where the stop belongs and what would prove it wrong;
* this module decides *how much*: position size comes from the risk budget, never
  from the model, and any level that breaks the stop band is thrown out.

Nothing the model writes reaches the database unchecked.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.fees.calculator import round_trip
from kestrel.indicators.engine import Series, compute
from kestrel.planning.builder import valid_until
from kestrel.services import bars as bars_service
from kestrel.services import config as config_service
from kestrel.services import rules as rule_service

log = logging.getLogger(__name__)

MIN_STOP_PCT = 0.015
MAX_STOP_PCT = 0.15
MIN_R = 1.5
MAX_PICKS = 6

SYSTEM = """你是一名谨慎的股票交易分析员。你只做多，不使用杠杆，接受"今天不交易"也是正确答案。

你的输出会被程序逐项校验，不合规的建议会被直接丢弃并记录原因。所以：
- 只能从给定的候选列表里选，code 必须原样照抄
- 止损必须在入场价下方 1.5% 到 15% 之间
- 目标价至少是入场到止损距离的 1.5 倍
- 不要给仓位建议，仓位由程序按风险预算计算

判断依据要具体，引用你看到的数字（涨幅、RSI、距离均线、量比、位置），
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


# --------------------------------------------------------------------- brief

def build_brief(markets: list[str], limit: int = 40, history: int = 260) -> dict:
    """A compact, honest snapshot: what the model is allowed to reason about."""
    conn = connect()
    try:
        settings = config_service.all_values(conn)
        rows = conn.execute(
            """
            SELECT s.id, s.market, s.code, s.name, s.sector, s.lot_size, s.pool_rank
            FROM symbol s
            LEFT JOIN watchlist w ON w.symbol_id = s.id
            WHERE s.market IN ({})
              AND (s.is_pool = 1 OR w.id IS NOT NULL)
            ORDER BY (w.id IS NULL), s.pool_rank
            LIMIT ?
            """.format(",".join("?" * len(markets))),
            (*markets, limit),
        ).fetchall()

        candidates = []
        for row in rows:
            bars = bars_service.cached_bars(conn, row["id"], history)
            if len(bars) < 80:
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
                    "vol_ratio": _round(ind.get("vol_ratio")),
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
                    "verdict": v.get("verdict"),
                    "expectancy_r": round(float(v.get("avg_exp") or 0), 3),
                    "edge_over_holding": round(float(v.get("avg_edge") or 0), 3),
                }
            )

    return {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "account": {
            "capital_cny": settings.get("account.capital"),
            "risk_per_trade_pct": settings.get("risk.per_trade_pct"),
            "max_per_sector": settings.get("risk.max_per_sector"),
        },
        "open_positions": positions,
        "_lots": lots,
        "candidates": candidates,
        "rule_evidence": evidence,
        "constraints": {
            "long_only": True,
            "min_stop_pct": MIN_STOP_PCT * 100,
            "max_stop_pct": MAX_STOP_PCT * 100,
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


# ------------------------------------------------------------------- provider

async def ask_model(brief: dict, cfg: dict, timeout: float = 90.0) -> dict:
    """Call an OpenAI-compatible endpoint and return parsed JSON."""
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
    """Models like to wrap JSON in fences. Accept that, reject everything else."""
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


# ----------------------------------------------------------------- validation

def validate_pick(raw: dict, brief: dict) -> tuple[Pick | None, str]:
    """Enforce every constraint the model was told about. Returns (pick, reason)."""
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

    for name, value in (("entry_low", entry_low), ("entry_high", entry_high),
                        ("stop", stop), ("target", target)):
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
    if not (MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT):
        return None, f"止损距离 {stop_pct * 100:.1f}% 超出 {MIN_STOP_PCT*100:.0f}%–{MAX_STOP_PCT*100:.0f}%"
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
            entry_low=entry_low,
            entry_high=entry_high,
            stop=stop,
            target=target,
            invalidation=str(raw.get("invalidation") or "")[:200],
            catalysts=[str(c)[:120] for c in (raw.get("catalysts") or [])][:4],
            risks=[str(c)[:120] for c in (raw.get("risks") or [])][:4],
        ),
        "",
    )


def size_position(pick: Pick, brief: dict, lot_size: int) -> tuple[int, float, float]:
    """Position size from the risk budget — the model never touches this."""
    capital = float(brief["account"]["capital_cny"])
    risk_pct = float(brief["account"]["risk_per_trade_pct"])
    fx = {"CNY": 1.0, "HKD": 0.92}.get(_currency(pick.market), 7.1)
    capital_local = capital / fx

    risk_budget = capital_local * risk_pct / 100
    risk_per_share = pick.entry_low - pick.stop
    if risk_per_share <= 0:
        return 0, 0.0, 0.0

    lot = max(1, lot_size)
    qty = int((risk_budget / risk_per_share) // lot) * lot
    cap_qty = int((capital_local * 0.35) / pick.entry_low // lot) * lot
    qty = min(qty, cap_qty)
    if qty <= 0:
        return 0, 0.0, 0.0

    cost = pick.entry_low * qty
    broker = None
    fees = round_trip(pick.market, _dec(cost), _dec(qty), broker)
    return qty, float(fees), float(risk_per_share * qty)


def _currency(market: str) -> str:
    return {"CN": "CNY", "HK": "HKD"}.get(market.upper(), "USD")


def _dec(value: float):
    from decimal import Decimal

    return Decimal(str(round(value, 4)))


# ---------------------------------------------------------------------- run

async def analyse(
    markets: list[str] | None = None,
    limit: int = 40,
    dry_reply: dict | None = None,
) -> dict:
    """Ask the model, validate everything, size it, store it."""
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

    # WorkBuddy has no synchronous entry point, so the request is queued and the
    # answer is collected later. Pretending otherwise would hang the page.
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
                f" 文件在 {bridge.PENDING}，处理完写到 {bridge.DONE}。"
            ),
            "bridge": bridge.bridge_info(),
        }

    raw_reply: dict
    try:
        if dry_reply is not None:
            raw_reply = dry_reply
        else:
            raw_reply = await ask_model(brief, cfg)
    except Exception as exc:  # noqa: BLE001
        _record_run(provider, model, brief, 0, 0, [], str(exc), None, started)
        return {"ok": False, "error": str(exc)}

    return _finish(brief, raw_reply, provider, model, started)


def _finish(brief: dict, raw_reply: dict, provider: str, model: str, started) -> dict:
    """Validate, size and store. Shared by the HTTP path and the file bridge."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_sector: dict[str, int] = {}
    max_sector = int(brief["account"].get("max_per_sector") or 2)

    picks = raw_reply.get("picks") or []
    if not isinstance(picks, list):
        picks = []

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
        # Can this account even afford one lot? Saying "no" beats dropping it.
        lots = brief.get("_lots") or {}
        qty, _fees, _risk = size_position(
            pick, brief, lots.get(f"{pick.market}:{pick.code}", 1)
        )
        if qty <= 0:
            rejected.append(
                {"code": pick.code, "reason": "本金不够买一手（或超出单票 35% 上限）"}
            )
            continue
        if sector:
            seen_sector[sector] = seen_sector.get(sector, 0) + 1
        accepted.append({"pick": pick, "sector": sector})
        if len(accepted) >= MAX_PICKS:
            break

    stored = 0
    if accepted:
        stored = _store(accepted, brief, model or provider, raw_reply)

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
    """Pick up answers the WorkBuddy agent has written and store them."""
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


def _store(accepted: list[dict], brief: dict, label: str, raw_reply: dict) -> int:
    conn = connect()
    saved = 0
    try:
        lots = {
            f"{r['market']}:{r['code']}": r["lot_size"]
            for r in conn.execute("SELECT market, code, lot_size FROM symbol").fetchall()
        }
        for item in accepted:
            pick: Pick = item["pick"]
            qty, fees, risk_amount = size_position(
                pick, brief, lots.get((pick.market, pick.code), 1)
            )
            if qty <= 0:
                continue
            symbol_id = conn.execute(
                "SELECT id FROM symbol WHERE market=? AND code=?", (pick.market, pick.code)
            ).fetchone()["id"]

            conn.execute(
                "UPDATE recommendation SET status='superseded'"
                " WHERE symbol_id=? AND status='active' AND source='ai'",
                (symbol_id,),
            )
            r_multiple = (pick.target - pick.entry_low) / (pick.entry_low - pick.stop)
            currency = _currency(pick.market)
            conn.execute(
                """
                INSERT INTO recommendation
                    (scan_id, symbol_id, market, code, name, currency, strategy, score,
                     entry_low, entry_high, stop, target1, target2, qty, lot_size,
                     risk_amount, est_fees, breakeven, r_multiple, confidence,
                     valid_until, invalidation, explain_zh, explain_en, status,
                     action, headline, learn_zh, learn_en,
                     source, ai_model, ai_thesis, ai_confidence)
                VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active',
                        ?,?,?,'', 'ai',?,?,?)
                """,
                (
                    symbol_id, pick.market, pick.code, pick.name, currency,
                    "ai", pick.conviction / 100.0,
                    to_micro(pick.entry_low), to_micro(pick.entry_high),
                    to_micro(pick.stop), to_micro(pick.target),
                    to_micro(pick.entry_low + (pick.target - pick.entry_low) * 1.5),
                    qty, lots.get((pick.market, pick.code), 1),
                    to_micro(risk_amount), to_micro(fees),
                    to_micro(pick.entry_low + fees / max(1, qty)),
                    round(r_multiple, 2), pick.conviction / 100.0,
                    valid_until(pick.market),
                    json.dumps([pick.invalidation or "model_invalidation"]),
                    pick.thesis, "",
                    pick.action,
                    pick.thesis,
                    _learn_text(pick),
                    label,
                    pick.thesis,
                    pick.conviction,
                ),
            )
            saved += 1
        conn.commit()
    finally:
        conn.close()
    return saved


def _learn_text(pick: Pick) -> str:
    risk_pct = (pick.entry_low - pick.stop) / pick.entry_low * 100
    return (
        f"这是什么：AI 选出的一笔做多计划，理由是「{pick.thesis}」。\n"
        f"为什么是它：{('；'.join(pick.catalysts) if pick.catalysts else '模型给出的判断见上方')}。\n"
        f"如果错了会怎样：跌破 {pick.stop:.2f}（约 −{risk_pct:.1f}%）就要走，"
        f"{pick.invalidation or '理由是它不再成立。'}"
    )


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
        rows = conn.execute("SELECT * FROM analyst_run ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
