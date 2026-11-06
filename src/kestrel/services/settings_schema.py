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
    Section("notify", "推送", "Notifications", [
        Field("notify.ntfy.enabled", "启用 ntfy", "ntfy", "switch"),
        Field("notify.ntfy.topic", "ntfy 主题", "ntfy topic", "text",
              help_zh="装 ntfy App，订阅同名主题即可收到，支持一键回执按钮",
              help_en="Subscribe to this topic in the ntfy app for one-tap receipts"),
        Field("notify.ntfy.server", "ntfy 服务器", "ntfy server", "text"),
        Field("notify.webhook.enabled", "启用 Webhook", "Webhook", "switch"),
        Field("notify.webhook.url", "Webhook 地址", "Webhook URL", "text",
              help_zh="企业微信群机器人地址可直接粘进来", help_en="WeCom bot URL works as-is"),
        Field("notify.weclawbot.enabled", "启用微信 clawbot", "WeChat clawbot", "switch",
              help_zh="走 WeClawBot-API 网关，只能在已登录的机器上推送",
              help_en="Requires a running WeClawBot-API gateway"),
        Field("notify.weclawbot.base_url", "网关地址", "Gateway URL", "text"),
        Field("notify.weclawbot.bot_id", "bot_id", "bot_id", "text"),
        Field("notify.weclawbot.api_token", "api_token", "api_token", "password"),
        Field("receipt.public_base", "回执链接前缀", "Receipt base URL", "text",
              help_zh="填手机能访问到的地址，局域网 IP 或 Tunnel 域名",
              help_en="A URL your phone can reach"),
    ]),
    Section("agents", "分析引擎", "Analysis engines", [
        Field("agent.mode", "模式", "Mode", "select", options=[
            ("rules", "纯规则（免费，默认）"), ("llm", "调用大模型"), ("workbuddy", "WorkBuddy 文件桥")]),
        Field("agent.llm.base_url", "接口地址", "Base URL", "text",
              help_zh="任何 OpenAI 兼容端点，比如 DeepSeek 或本地 Ollama",
              help_en="Any OpenAI-compatible endpoint"),
        Field("agent.llm.api_key", "API Key", "API Key", "password"),
        Field("agent.llm.model", "模型", "Model", "text"),
        Field("agent.llm.enabled", "用模型润色解释", "Polish explanations", "switch",
              help_zh="只改写文字，不改仓位和价格", help_en="Rewrites wording only, never sizes or prices"),
        Field("agent.budget_per_day", "每日调用上限", "Daily call budget", "number", step="10"),
    ]),
]

SECTION_MAP = {s.code: s for s in SECTIONS}

EXTRA_DEFAULTS: dict[str, Any] = {
    "ui.learning_mode": True,
    "ui.glossary": True,
    "scan.min_score": 0.55,
    "agent.mode": "rules",
    "agent.llm.base_url": "",
    "agent.llm.api_key": "",
    "agent.llm.model": "",
    "agent.llm.enabled": False,
    "agent.budget_per_day": 50,
    "notify.ntfy.enabled": False,
    "notify.ntfy.topic": "",
    "notify.ntfy.server": "https://ntfy.sh",
    "notify.webhook.enabled": False,
    "notify.webhook.url": "",
    "notify.weclawbot.enabled": False,
    "notify.weclawbot.base_url": "",
    "notify.weclawbot.bot_id": "",
    "notify.weclawbot.api_token": "",
}


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
