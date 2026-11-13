"""Declarative settings schema — the UI is generated from this, nothing is hard-coded."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kestrel.services import config as config_service


@dataclass(frozen=True)
class Field:
    key: str
    label_zh: str
    label_en: str
    kind: str = "text"          # text | number | switch | select | password
    options: list[tuple[str, str]] = field(default_factory=list)
    help_zh: str = ""
    help_en: str = ""
    step: str = "any"


@dataclass(frozen=True)
class Section:
    code: str
    label_zh: str
    label_en: str
    fields: list[Field]


SECTIONS: list[Section] = [
    Section("general", "通用", "General", [
        Field("account.capital", "账户本金", "Capital", "number", step="100",
              help_zh="所有仓位计算都以它为分母", help_en="Every size calculation divides by this"),
        Field("risk.per_trade_pct", "单笔风险上限（%）", "Risk per trade (%)", "number", step="0.5",
              help_zh="一笔最多亏本金的百分之几，建议 1–2", help_en="Percent of capital at risk per trade"),
        Field("risk.max_total_exposure_pct", "总仓位上限（%）", "Max exposure (%)", "number",
              step="5", help_zh="所有持仓加起来不超过本金的比例", help_en="Total exposure ceiling"),
        Field("ui.learning_mode", "学习模式", "Learning mode", "switch",
              help_zh="在每条建议下面展开「是什么 / 为什么 / 错了怎样」",
              help_en="Expand plain-language explanations under each plan"),
        Field("ui.glossary", "术语解释", "Glossary", "switch",
              help_zh="悬停或点击术语看解释，可单独关闭", help_en="Hover/click terms for definitions"),
    ]),
    Section("scan", "扫描", "Scanning", [
        Field("trade.horizon", "交易周期", "Trading horizon", "select",
              options=[("auto", "自动（按行情与资金推荐）"), ("short", "短线 · 日线"),
                       ("medium", "中线 · 周线"), ("long", "长线 · 月线（Beta）")],
              help_zh="决定扫描读哪种 K 线、回测持有多久、计划活多久；选自动则每次扫描前重新判断",
              help_en="Bar size for scans, backtests and plan lifetime; auto re-decides each scan"),
        Field("scan.min_score", "触发门槛（0–1）", "Min score", "number", step="0.05",
              help_zh="策略得分低于它就不生成建议，0.55 约等于中等强度",
              help_en="Strategies below this score are ignored"),
        Field("scan.auto_enabled", "自动扫描", "Scheduled scan", "switch",
              help_zh="打开后按下面的频率在后台跑", help_en="Run scans in the background"),
        Field("scan.interval_minutes", "扫描间隔（分钟）", "Interval (minutes)", "number", step="15"),
        Field("broker.HK", "港股券商", "HK broker", "select", options=[
            ("fosun_hk", "星财富（复星）"), ("usmart_hk", "盈立香港"),
            ("za_hk", "众安银行"), ("futu_hk", "富途"), ("tiger_hk", "老虎")]),
        Field("broker.US", "美股券商", "US broker", "select", options=[
            ("fosun_us", "星财富（复星）"), ("usmart_us", "盈立香港"),
            ("za_us", "众安银行"), ("ibkr_us", "IBKR")]),
        Field("broker.CN", "A 股券商", "CN broker", "select", options=[
            ("cn_generic", "通用 A 股（万 2.5）")]),
    ]),
    # 推送渠道不在这里：它们的字段在 services/channels.py 的 SCHEMA 里，
    # 存在 notify_channel 表，由设置页直接读写。这里的 notify.* 键曾经是
    # 第二套配置，存进去没人读——正是文档里说的「页面看起来能配其实无效」。
    Section("agents", "分析引擎", "Analysis engines", [
        # 键名必须和 engine.py / analyst.py 实际读的一致。历史上这里有第二套
        # agent.mode / agent.llm.* 键，存进去没人读，页面看起来能配其实无效。
        Field("agent.provider", "由谁产生建议", "Advice engine", "select", options=[
            ("rules", "规则引擎（免费，本地计算）"),
            ("llm", "大模型（实时，按 token 计费）"),
            ("workbuddy", "WorkBuddy（文件队列，异步）"),
        ]),
        Field("agent.base_url", "接口地址", "Base URL", "text",
              help_zh="任何 OpenAI 兼容端点，比如 DeepSeek 或本地 Ollama",
              help_en="Any OpenAI-compatible endpoint"),
        Field("agent.api_key", "API Key", "API Key", "password"),
        Field("agent.model", "模型名", "Model", "text"),
        Field("agent.daily_budget", "每天最多调用", "Daily call budget", "number", step="10"),
        Field("bridge.root", "WorkBuddy 队列目录", "WorkBuddy queue directory", "text",
              help_zh="留空则跟随数据目录。容器部署时填一个宿主机也能访问的共享目录。",
              help_en="Empty follows the data directory. In containers, point this at a shared path."),
    ]),
]

SECTION_MAP = {s.code: s for s in SECTIONS}

EXTRA_DEFAULTS: dict[str, Any] = {
    "ui.learning_mode": True,
    "ui.glossary": True,
    "scan.min_score": 0.55,
}

# 旧版本写过这些键，但它们不代表任何真实行为。清理掉，免得用户以为设过什么。
LEGACY_KEYS = (
    "agent.mode",
    "agent.llm.base_url",
    "agent.llm.api_key",
    "agent.llm.model",
    "agent.llm.enabled",
    "agent.budget_per_day",
    # 推送渠道曾经有两套配置：这里的 notify.* 和 notify_channel 表。
    # 只有后者被真正读取，前面这些存进去不代表任何行为。
    "notify.ntfy.enabled",
    "notify.ntfy.topic",
    "notify.ntfy.server",
    "notify.webhook.enabled",
    "notify.webhook.url",
    "notify.weclawbot.enabled",
    "notify.weclawbot.base_url",
    "notify.weclawbot.bot_id",
    "notify.weclawbot.api_token",
)


def defaults() -> dict[str, Any]:
    merged = dict(config_service.DEFAULTS)
    merged.update(EXTRA_DEFAULTS)
    return merged


def read_all(conn) -> dict[str, Any]:
    merged = defaults()
    merged.update(config_service.all_values(conn))
    return merged


def coerce(field: Field, raw: str) -> Any:
    if field.kind == "switch":
        return raw in ("1", "true", "on", "yes")
    if field.kind == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0
    return raw
