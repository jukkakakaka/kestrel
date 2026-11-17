"""右侧炒股 Agent 助手服务（Kestrel Copilot）。

核心职责：
1. **强前置门禁**：必须先配置可用大模型 API（``agent.base_url`` + ``agent.model``）方可使用；
   支持在侧边栏内联一键选择常用 OpenAI 兼容服务商并保存配置。
2. **双层严格作用域控制**：只允许围绕「Kestrel 本产品使用与股票交易决策（账户资金、持仓、
   建议单、行情技术面、风控纪律、交易规则、复盘归因）」展开对话。所有无关领域提问（编程、
   娱乐、烹饪、情感、闲聊、越狱等）均由本地规则守卫 + System Prompt 双层拒绝。
3. **实时实盘感知上下文**：每次提问自动注入当前动态资金账本、未平仓持仓与退出建议、
   活跃买单计划、多市场周期顾问状态，以及当前正在浏览的个股 K 线技术指标。
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from typing import Any

import httpx

from kestrel.db.engine import connect, transaction
from kestrel.services import config as config_service

log = logging.getLogger(__name__)

# 常用 OpenAI 兼容模型接口预设，方便用户在右侧栏一键配置。
PRESETS: list[dict[str, str]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "hint_zh": "性价比高、中文理解强，需填写 DeepSeek API Key",
        "hint_en": "Fast & cost-effective; requires a DeepSeek API key",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "hint_zh": "OpenAI 官方接口",
        "hint_en": "Official OpenAI API",
    },
    {
        "id": "qwen",
        "name": "通义千问 (DashScope)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "hint_zh": "阿里云百炼 OpenAI 兼容接口",
        "hint_en": "Alibaba Cloud DashScope OpenAI-compatible API",
    },
    {
        "id": "moonshot",
        "name": "Kimi (Moonshot)",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "hint_zh": "月之暗面开放平台",
        "hint_en": "Moonshot Open Platform",
    },
    {
        "id": "ollama",
        "name": "Ollama (本地模型)",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:14b",
        "hint_zh": "本地私有运行，API Key 可留空",
        "hint_en": "Local Ollama server; API key optional",
    },
]

_JAILBREAK_RE = re.compile(
    r"(ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)"
    r"|system\s+prompt|developer\s+mode|jailbreak|act\s+as\s+a\s+(?!trader|analyst|broker)"
    r"|忽略(之前|以上|所有)(指令|规则|设定)|忘记(所有)?(规则|设定)|越狱|扮演(?!.*(交易员|分析师|顾问|炒股|Kestrel)))",
    re.IGNORECASE,
)

_OFF_TOPIC_RE = re.compile(
    r"(菜谱|做饭|红烧肉|烹饪|星座|算命|塔罗|恋爱|表白|撩妹|分手|旅游攻略|机票|酒店|菜品|"
    r"写(一首|首)?诗|散文|小说|笑话|段子|游戏攻略|王者荣耀|原神|吃鸡|电影推荐|电视剧|八卦|天气预报|"
    r"谁是.*总统|首都是|作业答案|数学题|物理题|化学题|"
    r"写(个|段|一个)?(python|java|js|javascript|html|css|sql|c\+\+|爬虫|脚本|代码)|"
    r"\b(recipe|cook|horoscope|astrology|dating|poem|poetry|joke|movie|anime|game guide|"
    r"write\s+(me\s+)?a\s+(story|poem|essay|song|code|script)|weather\s+forecast|capital\s+of|"
    r"homework|leetcode|fibonacci|sudoku)\b)",
    re.IGNORECASE,
)

_DOMAIN_TERMS = (
    # 中文核心交易与产品词汇
    "kestrel", "茶隼", "炒股", "股票", "个股", "大盘", "市场", "a股", "港股", "美股",
    "科创板", "北交所", "主板", "创业板", "etf", "ipo", "打新", "新股",
    "买", "卖", "下单", "挂单", "委托", "限价", "市价", "成交", "平仓", "建仓", "加仓", "减仓",
    "持仓", "仓位", "敞口", "底仓", "风险", "风控", "止损", "止盈", "保本", "移动止损", "时间止损",
    "资金", "本金", "现金", "总资产", "净值", "盈亏", "浮盈", "浮亏", "回撤", "收益", "亏损", "校准", "出入金",
    "建议", "扫描", "策略", "规则", "周期", "频率", "短线", "中线", "长线", "快进快出",
    "k线", "均线", "趋势", "牛市", "熊市", "震荡", "突破", "回踩", "放量", "缩量", "量比", "跳空",
    "atr", "rsi", "macd", "sma", "vcp", "r倍数", "盈亏比", "期望值", "胜率", "滑点",
    "t+1", "t+0", "整手", "碎股", "小数股", "手续费", "佣金", "平台费", "印花税", "过户费",
    "券商", "对账单", "复盘", "归因", "破规", "回测", "数据源", "行情", "词典", "术语", "配置", "设置", "api",
    "证伪", "置换检验", "p值", "夏普", "参数高原", "过拟合", "偏离", "样本",
    # 英文核心交易与产品词汇
    "stock", "trade", "trading", "equity", "cash", "portfolio", "position", "positions",
    "buy", "sell", "order", "limit", "market", "fill", "filled", "stop", "target", "trailing",
    "drawdown", "pnl", "p&l", "risk", "sizing", "size", "exposure", "lot", "fractional",
    "fee", "fees", "commission", "stamp", "slippage", "breakeven", "break-even",
    "advice", "scan", "strategy", "rule", "horizon", "regime", "bull", "bear", "sideways",
    "candle", "chart", "volume", "breakout", "pullback", "backtest", "journal", "calibrate",
    "calibration", "statement", "glossary", "broker", "tick", "quote", "watchlist", "ticker",
    "falsification", "permutation", "p-value", "p_value", "sharpe", "plateau", "overfit", "drift",
)

_GREETING_OR_FOLLOWUP_RE = re.compile(
    r"^(你好|您好|嗨|hi|hello|hey|help|帮助|怎么用|如何使用|你能做什么|你能帮我什么|"
    r"详细说说|展开讲讲|再解释一下|举个例子|还有呢|然后呢|为什么\??|why\??|explain\s+more)\s*[!！?？。.]*$",
    re.IGNORECASE,
)

_TICKER_RE = re.compile(r"(?:\b\d{5,6}\b|\$[A-Za-z]{1,6}|\b[A-Z]{2,5}(?:\.[A-Z])?\b)")


def status() -> dict:
    """读取大模型 API 配置状态，决定右侧助手是否已解锁对话。"""
    conn = connect()
    try:
        base_url = str(config_service.get(conn, "agent.base_url", "") or "").strip()
        model = str(config_service.get(conn, "agent.model", "") or "").strip()
        api_key = str(config_service.get(conn, "agent.api_key", "") or "").strip()
        provider = str(config_service.get(conn, "agent.provider", "rules") or "rules")
    finally:
        conn.close()

    configured = bool(base_url and model)
    masked = ""
    if api_key:
        masked = api_key[:4] + "..." + api_key[-4:] if len(api_key) > 10 else "***"
    return {
        "configured": configured,
        "base_url": base_url,
        "model": model,
        "has_key": bool(api_key),
        "api_key_masked": masked,
        "provider": provider,
        "presets": PRESETS,
    }


def quick_config(
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
    set_engine_llm: bool = False,
) -> dict:
    """在右侧边栏直接保存或更新大模型 API 凭据。"""
    clean_url = str(base_url or "").strip().rstrip("/")
    clean_model = str(model or "").strip()
    if not clean_url or not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        return {"ok": False, "error": "请填写合法的 API 接口地址（需以 https:// 或 http:// 开头）"}
    if not clean_model:
        return {"ok": False, "error": "请填写模型名称（例如 deepseek-chat / gpt-4o-mini）"}

    conn = connect()
    try:
        with transaction(conn):
            config_service.put(conn, "agent.base_url", clean_url)
            config_service.put(conn, "agent.model", clean_model)
            if api_key is not None and str(api_key).strip() != "":
                config_service.put(conn, "agent.api_key", str(api_key).strip())
            elif api_key == "":
                # 允许本地 Ollama 清空 Key；但如果表单没回传或保留原 Key，传 None 表示不变。
                config_service.put(conn, "agent.api_key", "")
            if set_engine_llm:
                config_service.put(conn, "agent.provider", "llm")
        return {"ok": True, "status": status()}
    except Exception as exc:  # noqa: BLE001
        log.exception("copilot quick_config failed")
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def history(session_id: str = "default", limit: int = 40) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, role, content, page_ctx, rejected, created_at"
            " FROM copilot_message WHERE session_id=?"
            " ORDER BY id DESC LIMIT ?",
            (session_id, max(1, limit)),
        ).fetchall()
        out = [dict(r) for r in reversed(rows)]
        return out
    finally:
        conn.close()


def clear_history(session_id: str = "default") -> None:
    conn = connect()
    try:
        with transaction(conn):
            conn.execute("DELETE FROM copilot_message WHERE session_id=?", (session_id,))
    finally:
        conn.close()


def _has_active_history(session_id: str) -> bool:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM copilot_message WHERE session_id=? AND rejected=0 LIMIT 1",
            (session_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def check_domain_scope(
    message: str, *, locale: str = "zh-CN", has_history: bool = False
) -> tuple[bool, str]:
    """第一道本地话题守卫：拦截任何非炒股/非 Kestrel 产品的对话与越狱指令。"""
    en = str(locale).lower().startswith("en")
    text = str(message or "").strip()
    if not text:
        return False, "Please enter a question about trading or Kestrel." if en else "请输入与股票交易或 Kestrel 功能相关的问题。"
    if len(text) > 4000:
        return False, "Message is too long (max 4,000 chars)." if en else "单次提问过长，请精简到 4000 字以内。"

    if _JAILBREAK_RE.search(text):
        return False, (
            "I am Kestrel's dedicated stock-trading assistant and cannot override my role. "
            "Ask me about your portfolio, signals, trading rules, or Kestrel settings."
            if en
            else "我是 Kestrel 专属炒股与风控助手，无法切换角色或忽略交易安全准则。你可以继续向我提问持仓、交易计划、股市规则或 Kestrel 使用问题。"
        )

    if _OFF_TOPIC_RE.search(text):
        return False, (
            "I can only answer questions related to stock trading and Kestrel (your portfolio, "
            "recommendations, market rules, risk sizing, and platform features)."
            if en
            else "抱歉，我仅支持解答与**股票交易**或 **Kestrel 产品本身**相关的问题（如账户资金校准、持仓止损、操作建议解读、A/港/美股交易规则、回测与设置）。"
        )

    lower = text.lower()
    has_domain = any(term in lower for term in _DOMAIN_TERMS) or bool(_TICKER_RE.search(text))
    if has_domain:
        return True, ""

    # 允许开场白和已有合规上下文下的简短追问，其余一律明确说明服务边界。
    if _GREETING_OR_FOLLOWUP_RE.match(text) and (
        has_history or not re.search(r"为什么|why|详细|展开|例子|还有|然后", lower)
    ):
        return True, ""

    return False, (
        "This assistant is strictly scoped to stock trading and Kestrel features. "
        "Try asking about your cash balance, open positions, a ticker's setup, fees, or A/HK/US rules."
        if en
        else "当前对话助手仅限于解答**股票交易决策、市场制度与 Kestrel 产品功能**。你可以这样问我：\n"
             "• 「帮我评估一下当前持仓风险和止损位置」\n"
             "• 「这笔建议怎么在券商 App 下限价单？」\n"
             "• 「按我现在的资金推荐什么市场和周期？」\n"
             "• 「我的可用现金或持仓数量不对该怎么校准？」"
    )


def _extract_symbol_hint(page_path: str, symbol_hint: str) -> tuple[str, str] | None:
    if symbol_hint and ":" in symbol_hint:
        mkt, code = symbol_hint.split(":", 1)
        return mkt.strip().upper(), code.strip()
    m = re.search(r"/stock/(CN|HK|US)/([^/?#]+)", page_path or "", re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None


def build_context(
    *, page_path: str = "", symbol_hint: str = "", locale: str = "zh-CN"
) -> dict[str, Any]:
    """实时采集当前账户资金、持仓、活跃建议、市场顾问与当前浏览股票的完整上下文。"""
    from kestrel.indicators.engine import Series, compute
    from kestrel.services import advisor, backtest, portfolio, positions, scan
    from kestrel.services import bars as bars_service
    from kestrel.services import rules as rule_service

    snap = portfolio.snapshot(include_quotes=True, limit_ledger=5)
    open_pos = positions.open_positions(include_quotes=True, with_exits=True)
    recs = scan.active_recommendations(limit=8)
    try:
        eval_info = advisor.evaluate(force=False)
    except Exception:  # noqa: BLE001
        eval_info = {}
    market_rec = advisor.recommend_markets(capital_cny=Decimal(str(snap["total_equity_cny"])))

    pos_compact = [
        {
            "trade_id": p["trade_id"],
            "market": p["market"],
            "code": p["code"],
            "name": p["name"],
            "qty": float(p["qty"]),
            "entry": float(p["entry"]),
            "price": float(p["price"]) if p["price"] is not None else None,
            "stop": float(p["stop"]) if p["stop"] is not None else None,
            "target": float(p["target"]) if p["target"] is not None else None,
            "pnl": float(p["pnl"]) if p["pnl"] is not None else None,
            "pnl_pct": round(p["pnl_pct"], 2) if p["pnl_pct"] is not None else None,
            "r_now": round(p["r_now"], 2) if p["r_now"] is not None else None,
            "state": p["state"],
            "can_sell_today": p.get("can_sell_today", True),
            "exit_action": (p.get("exit") or {}).get("action"),
            "exit_reason": (p.get("exit") or {}).get("reason_zh"),
        }
        for p in open_pos[:12]
    ]

    recs_compact = [
        {
            "rec_id": r["id"],
            "market": r["market"],
            "code": r["code"],
            "name": r["name"],
            "strategy": r.get("strategy"),
            "horizon": r.get("horizon"),
            "entry_low": round(r["entry_low"] / 1_000_000, 3),
            "entry_high": round((r.get("entry_high") or r["entry_low"]) / 1_000_000, 3),
            "stop": round(r["stop"] / 1_000_000, 3),
            "target1": round(r["target1"] / 1_000_000, 3),
            "qty": r["qty"],
            "est_fees": round((r.get("est_fees") or 0) / 1_000_000, 2),
            "breakeven": round((r.get("breakeven") or 0) / 1_000_000, 3),
            "r_multiple": r.get("r_multiple"),
        }
        for r in recs[:8]
    ]

    stock_detail: dict[str, Any] | None = None
    pair = _extract_symbol_hint(page_path, symbol_hint)
    if pair is not None:
        mkt, code = pair
        conn = connect()
        try:
            sym = conn.execute(
                "SELECT id, market, code, name, lot_size FROM symbol WHERE market=? AND code=?",
                (mkt, code),
            ).fetchone()
            if sym is not None:
                bars = bars_service.cached_bars(conn, sym["id"], 90)
                if bars:
                    ind = compute(Series.from_bars(bars))
                    stock_detail = {
                        "market": sym["market"],
                        "code": sym["code"],
                        "name": sym["name"],
                        "lot_size": sym["lot_size"],
                        "close": round(float(ind.get("close") or 0), 3),
                        "rsi14": round(float(ind.get("rsi14") or 0), 1) if ind.get("rsi14") else None,
                        "atr14": round(float(ind.get("atr14") or 0), 3) if ind.get("atr14") else None,
                        "sma20": round(float(ind.get("sma20") or 0), 3) if ind.get("sma20") else None,
                        "sma60": round(float(ind.get("sma60") or 0), 3) if ind.get("sma60") else None,
                        "vol_today_ratio": round(float(ind.get("vol_today_ratio") or 0), 2)
                        if ind.get("vol_today_ratio")
                        else None,
                        "vcp_ratio": round(float(ind.get("vcp_ratio") or 0), 2)
                        if ind.get("vcp_ratio")
                        else None,
                    }
        finally:
            conn.close()

    # 汇总各策略四关证伪状态与近期回测记录（「Python 算数字，AI 做研究」）
    strategy_falsification: list[dict[str, Any]] = []
    try:
        drift_map = rule_service.live_drift_report()
        for r_item in rule_service.all_rules():
            val = r_item.get("validation") or {}
            d_info = drift_map.get(r_item["code"], {})
            strategy_falsification.append(
                {
                    "code": r_item["code"],
                    "name": r_item["name"],
                    "enabled": bool(r_item.get("enabled", 1)),
                    "verdict": val.get("verdict") or "未验证/样本不足",
                    "timeframe": val.get("timeframe"),
                    "avg_exp": val.get("avg_exp"),
                    "avg_edge": val.get("avg_edge"),
                    "p_value": val.get("p_value"),
                    "drift_status": d_info.get("status", "insufficient"),
                    "drift_warning": bool(d_info.get("warning", False)),
                    "live_samples": d_info.get("sample_size", 0),
                    "live_avg_r": d_info.get("actual_avg_r"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("copilot falsification context skipped: %s", exc)

    recent_bt_compact: list[dict[str, Any]] = []
    try:
        for bt in backtest.recent(limit=5):
            recent_bt_compact.append(
                {
                    "market": bt["market"],
                    "code": bt["code"],
                    "strategy": bt["strategy"],
                    "timeframe": bt.get("timeframe"),
                    "trades": bt["trades"],
                    "win_rate": round(float(bt["win_rate"] or 0) * 100, 1),
                    "avg_r": round(float(bt["avg_r"] or 0), 3),
                    "max_dd": round(float(bt["max_dd"] or 0), 2),
                }
            )
    except Exception:  # noqa: BLE001
        pass

    return {
        "page_path": page_path or "/",
        "portfolio": {
            "initial_capital_cny": snap["initial_capital_cny"],
            "available_cash_cny": snap["available_cash_cny"],
            "market_value_cny": snap["market_value_cny"],
            "total_equity_cny": snap["total_equity_cny"],
            "realized_pnl_cny": snap["realized_pnl_cny"],
            "unrealized_pnl_cny": snap["unrealized_pnl_cny"],
            "exposure_pct": snap["exposure_pct"],
            "exposure_cap_pct": snap["exposure_cap_pct"],
            "open_count": snap["open_count"],
        },
        "advisor": {
            "recommended_horizon": eval_info.get("horizon") or market_rec.get("recommended_horizon"),
            "horizon_reason": eval_info.get("reason") or "",
            "market_regimes": eval_info.get("market_regimes") or {},
            "recommended_markets": market_rec.get("recommended_markets"),
            "market_reasons": market_rec.get("reasons"),
        },
        "positions": pos_compact,
        "active_advice": recs_compact,
        "strategy_falsification": strategy_falsification,
        "recent_backtests": recent_bt_compact,
        "focused_stock": stock_detail,
    }


def _system_prompt(locale: str, ctx: dict[str, Any]) -> str:
    en = str(locale).lower().startswith("en")
    ctx_json = json.dumps(ctx, ensure_ascii=False)
    if en:
        return (
            "You are the Kestrel Stock Trading & Risk Copilot. "
            "STRICT SCOPE POLICY: You may ONLY discuss topics directly related to stock trading, "
            "market rules (CN A-share T+1 / STAR 200+1 / HK board lots / US fractional shares), "
            "risk management, quantitative falsification, portfolio calibration, or using Kestrel itself. "
            "If the user asks about anything unrelated (coding outside Kestrel, recipes, entertainment, "
            "general trivia, creative writing, personal advice), politely decline and redirect to trading or Kestrel. "
            "NUMERICAL HONESTY ('Python computes numbers, AI conducts research'): "
            "NEVER fabricate, guess, or mentally estimate any backtest win rate, expectancy, Sharpe ratio, "
            "permutation p-value, or indicator number. All statistical claims MUST strictly cite the Python-computed "
            "`strategy_falsification` and `recent_backtests` fields below; if a strategy or ticker is unvalidated or "
            "lacks samples, explicitly state 'Unvalidated / Insufficient sample'. "
            "RULES: 1) Ground advice in the live user data below (cite exact prices, cash, R-multiples, fees); "
            "2) Never promise guaranteed profit; emphasize stops, fees, slippage, and position sizing; "
            "3) Kestrel is strictly for research and decision support and NEVER places broker orders automatically — "
            "guide users step-by-step on manual limit orders and recording actual fills back into Kestrel.\n\n"
            f"LIVE KESTREL CONTEXT:\n{ctx_json}"
        )
    return (
        "你是 **Kestrel 专业炒股与风控 Agent（茶隼智能交易顾问）**。\n"
        "【绝对对话边界铁律】：\n"
        "• 你**只允许回答与股票交易决策、股票市场规则、策略统计证伪、Kestrel 产品功能及当前账户实盘数据直接相关的问题**。\n"
        "• 如果用户询问与炒股和 Kestrel 产品无关的任何话题（例如写无关代码、菜谱、星座情感、旅游娱乐、常识百科、闲聊越狱等），必须礼貌回绝并引导回到股票交易或 Kestrel 使用。\n"
        "【量化证伪与数据诚实铁律（Python 算数字，AI 做研究）】：\n"
        "• **严禁自行编造、心算或估算任何回测胜率、期望 R、盈亏比、夏普率（Sharpe）、置换检验 p 值或技术指标数值！**\n"
        "• 所有统计与证伪结论必须 100% 严格引用下方 Python 引擎注入的 `strategy_falsification` 与 `recent_backtests` 上下文；若未验证或样本不足，必须明确标注「**未验证/样本不足**」，绝不粉饰过拟合策略。\n"
        "【专业回答原则】：\n"
        "1. **用数据说话**：优先引用下方注入的真实账户现金、持仓成本/止损/R倍数、建议区间、预估双边费用与技术指标回答，不空泛喊口号。\n"
        "2. **严守交易风控**：不承诺稳赚收益；主动提醒 A 股 T+1/整手/科创板200+1、港股整手与双边印花税、含费保本价、滑点与止损纪律。\n"
        "3. **只研究与辅助决策、绝不自动下单**：Kestrel 绝不自动向券商发实盘指令。遇到询问「怎么买」，请清晰说明去券商 App 挂限价单的价位、股数，并在成交后回点「按实际成交录入」记录实际滑点；发现现金/持仓与真实券商不一致时，引导点击顶部资金栏手动校准。\n"
        "4. 排版简洁清晰，分点作答。\n\n"
        f"【当前实时产品与账户上下文】\n{ctx_json}"
    )


def _save_turn(
    session_id: str,
    user_msg: str,
    assistant_reply: str,
    page_path: str,
    *,
    rejected: bool = False,
) -> None:
    conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO copilot_message (session_id, role, content, page_ctx, rejected)"
                " VALUES (?,?,?,?,?)",
                (session_id, "user", user_msg, page_path or None, 1 if rejected else 0),
            )
            conn.execute(
                "INSERT INTO copilot_message (session_id, role, content, page_ctx, rejected)"
                " VALUES (?,?,?,?,?)",
                (session_id, "assistant", assistant_reply, page_path or None, 1 if rejected else 0),
            )
    finally:
        conn.close()


def chat(
    message: str,
    *,
    page_path: str = "",
    symbol_hint: str = "",
    session_id: str = "default",
    locale: str = "zh-CN",
) -> dict:
    """执行一次带上下文与安全边界的 Agent 对话。"""
    en = str(locale).lower().startswith("en")
    cfg = status()
    if not cfg["configured"]:
        return {
            "ok": False,
            "code": "not_configured",
            "error": (
                "Configure an LLM API endpoint and model name first to activate Copilot."
                if en
                else "请先配置大模型 API 地址与模型名称，再启用炒股 Agent 对话助手。"
            ),
        }

    text = str(message or "").strip()
    active_hist = _has_active_history(session_id)
    allowed, reject_msg = check_domain_scope(text, locale=locale, has_history=active_hist)
    if not allowed:
        _save_turn(session_id, text or "(empty)", reject_msg, page_path, rejected=True)
        return {
            "ok": True,
            "rejected": True,
            "reply": reject_msg,
        }

    ctx = build_context(page_path=page_path, symbol_hint=symbol_hint, locale=locale)
    prior = [
        {"role": row["role"], "content": row["content"]}
        for row in history(session_id=session_id, limit=20)
        if not row.get("rejected") and row["role"] in ("user", "assistant")
    ][-10:]

    messages = [
        {"role": "system", "content": _system_prompt(locale, ctx)},
        *prior,
        {"role": "user", "content": text},
    ]

    conn = connect()
    try:
        api_key = str(config_service.get(conn, "agent.api_key", "") or "").strip()
        temperature = float(config_service.get(conn, "agent.temperature", 0.3) or 0.3)
    finally:
        conn.close()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    endpoint = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": min(max(temperature, 0.0), 0.7),
    }

    try:
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
            if resp.status_code >= 300:
                return {
                    "ok": False,
                    "code": "upstream_error",
                    "error": (
                        f"Model API returned {resp.status_code}: {resp.text[:240]}"
                        if en
                        else f"大模型接口返回错误 {resp.status_code}：{resp.text[:240]}"
                    ),
                }
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("copilot API request failed: %s", exc)
        return {
            "ok": False,
            "code": "network_error",
            "error": (
                f"Failed to reach model API: {exc}"
                if en
                else f"请求大模型接口失败：{exc}"
            ),
        }

    reply = str(
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()
    if not reply:
        return {
            "ok": False,
            "code": "empty_reply",
            "error": "Model returned an empty response." if en else "模型返回内容为空，请重试。",
        }

    _save_turn(session_id, text, reply, page_path, rejected=False)
    return {
        "ok": True,
        "rejected": False,
        "reply": reply,
        "context_summary": {
            "equity_cny": ctx["portfolio"]["total_equity_cny"],
            "cash_cny": ctx["portfolio"]["available_cash_cny"],
            "positions": len(ctx["positions"]),
            "advice": len(ctx["active_advice"]),
            "focused": (
                f"{ctx['focused_stock']['name']} ({ctx['focused_stock']['code']})"
                if ctx.get("focused_stock")
                else None
            ),
        },
    }
