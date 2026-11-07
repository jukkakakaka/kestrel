"""One entry point for producing advice, whatever engine is configured.

Having two buttons side by side ("scan" and "ask the AI") made the user guess
which one was authoritative. There is only one: whatever ``agent.provider`` says.
The rules engine stays in the product as the thing that is measured and studied,
and as the fallback when no model is configured — not as a parallel truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kestrel.db.engine import connect
from kestrel.services import analyst, bridge, scan
from kestrel.services import config as config_service

log = logging.getLogger(__name__)

RULES = "rules"
LLM = "llm"
WORKBUDDY = "workbuddy"

LABELS = {
    RULES: "规则引擎",
    LLM: "大模型",
    WORKBUDDY: "WorkBuddy",
}


@dataclass(slots=True)
class EngineStatus:
    provider: str
    label: str
    is_ai: bool
    configured: bool
    reason: str
    model: str
    pending: int
    button: str
    banner: str

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "label": self.label,
            "is_ai": self.is_ai,
            "configured": self.configured,
            "reason": self.reason,
            "model": self.model,
            "pending": self.pending,
            "button": self.button,
            "banner": self.banner,
        }


def status() -> EngineStatus:
    conn = connect()
    try:
        provider = str(config_service.get(conn, "agent.provider", RULES) or RULES)
        model = str(config_service.get(conn, "agent.model", "") or "")
        base_url = str(config_service.get(conn, "agent.base_url", "") or "")
        provider = provider if provider in LABELS else RULES
    finally:
        conn.close()

    pending = bridge.pending_count() if provider == WORKBUDDY else 0

    if provider == RULES:
        return EngineStatus(
            provider=RULES,
            label=LABELS[RULES],
            is_ai=False,
            configured=True,
            reason="",
            model="",
            pending=0,
            button="开始扫描",
            banner=(
                "当前由规则引擎产生建议：按固定条件筛选，每条规则都经过历史验证，"
                "测下来没有优势的会被自动停用。"
            ),
        )

    if provider == LLM:
        configured = bool(base_url and model)
        return EngineStatus(
            provider=LLM,
            label=LABELS[LLM],
            is_ai=True,
            configured=configured,
            reason="" if configured else "还没有填写接口地址和模型名",
            model=model,
            pending=0,
            button="开始分析" if configured else "先配置模型",
            banner=(
                f"当前由大模型（{model or '未配置'}）分析并选股：它看到候选股的完整指标、"
                "你的账户和已有规则的实测结果，自己决定买什么、为什么。"
                "仓位和风控仍由本地代码强制执行。"
            ),
        )

    return EngineStatus(
        provider=WORKBUDDY,
        label=LABELS[WORKBUDDY],
        is_ai=True,
        # the file bridge needs no credentials; it always "works", it is just slow
        configured=True,
        reason="",
        model="workbuddy",
        pending=pending,
        button="交给 WorkBuddy 分析",
        banner=(
            "当前由 WorkBuddy 分析：任务写入队列，由 WorkBuddy 的定时任务读取后给出建议，"
            f"结果自动回填。队列里现有 {pending} 条未处理。"
        ),
    )


async def run(markets: list[str] | None = None, trigger: str = "manual") -> dict:
    """Produce advice with whichever engine is configured."""
    current = status()
    if current.provider == RULES:
        result = await scan.run_scan(trigger=trigger, markets=markets)
        result["engine"] = RULES
        return result

    if current.provider == LLM and not current.configured:
        return {
            "ok": False,
            "engine": LLM,
            "error": "大模型还没配置：设置 → 分析引擎，填接口地址和模型名。",
        }

    result = await analyst.analyse(markets=markets)
    result["engine"] = current.provider
    return result
