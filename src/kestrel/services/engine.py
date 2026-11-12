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
    # 英文界面的按钮与横幅。页面上直接显示中文句子是「翻译漏了」的味道，
    # 所以状态对象同时带两套文案。
    label_en: str = ""
    button_en: str = ""
    banner_en: str = ""
    reason_en: str = ""

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

    def localised(self, locale: str) -> EngineStatus:
        """按语言取一份文案。返回浅拷贝，避免调用方互相污染。"""
        if not str(locale).lower().startswith("en"):
            return self
        return EngineStatus(
            provider=self.provider,
            label=self.label_en or self.label,
            is_ai=self.is_ai,
            configured=self.configured,
            reason=self.reason_en or self.reason,
            model=self.model,
            pending=self.pending,
            button=self.button_en or self.button,
            banner=self.banner_en or self.banner,
        )


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
            label_en="Rule engine",
            button_en="Start scan",
            banner_en=(
                "Advice comes from the rule engine: fixed conditions, every rule measured, "
                "and anything without demonstrated edge is switched off automatically."
            ),
        )

    if provider == LLM:
        configured = bool(base_url and model)
        budget_hint = ""
        try:
            from kestrel.services import analyst

            conn = connect()
            try:
                cfg = config_service.all_values(conn)
            finally:
                conn.close()
            allowed, used = analyst.budget_state(cfg)
            limit = int(cfg.get("agent.daily_budget") or 0)
            if limit > 0:
                budget_hint = f"今天已用 {used}/{limit} 次。"
                if not allowed:
                    budget_hint += "额度已用完，调高上限或明天再来。"
        except Exception:  # noqa: BLE001
            budget_hint = ""
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
                f"仓位和风控仍由本地代码强制执行。{budget_hint}"
            ),
            label_en="Model",
            button_en="Start analysis" if configured else "Configure a model first",
            banner_en=(
                f"A model ({model or 'not configured'}) picks from the full indicator set, "
                "your account and each rule's measured record. Risk and sizing stay in local code."
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
        label_en="WorkBuddy",
        button_en="Send to WorkBuddy",
        banner_en=(
            "WorkBuddy picks: tasks are written to a queue, a scheduled job answers them, "
            f"and the result is ingested automatically. {pending} request(s) pending."
        ),
    )


async def run(markets: list[str] | None = None, trigger: str = "manual") -> dict:
    """Produce advice with whichever engine is configured."""
    current = status()
    if current.provider == RULES:
        result = await scan.run_scan(trigger=trigger, markets=markets)
        result["engine"] = RULES
        await _notify_new(result)
        return result

    if current.provider == LLM and not current.configured:
        return {
            "ok": False,
            "engine": LLM,
            "error": "大模型还没配置：设置 → 分析引擎，填接口地址和模型名。",
        }

    before = _max_rec_id()
    result = await analyst.analyse(markets=markets)
    result["engine"] = current.provider
    await _notify_new(result, since=before)
    return result


def _max_rec_id() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM recommendation").fetchone()
        return int(row["n"])
    finally:
        conn.close()


async def _notify_new(result: dict, since: int = 0) -> None:
    """把本次真正新产生的建议推出去。

    早期版本只推「最近 10 条 active」，既可能重复推旧的，也会漏掉第 11 条；
    而 AI 路径干脆没有推送。用「本次新增的 id」来界定，两条路径就一致了。
    """
    if not result.get("ok") or result.get("pending"):
        return
    from kestrel.notify import dispatcher

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id FROM recommendation WHERE status='active' AND id > ?"
            " ORDER BY id DESC LIMIT 20",
            (since,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    queued = 0
    for row in rows:
        queued += dispatcher.queue(int(row["id"]))
    result["queued"] = queued
    if queued:
        result["notify"] = await dispatcher.dispatch()
        await dispatcher.dispatch_outbox()
