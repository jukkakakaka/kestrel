"""Data source registry: which providers exist, and in what order they run.

Sources are configurable because they are unofficial public endpoints: any one
of them can start rate-limiting or change shape, and the user needs a way to
turn one off without editing code.
"""

from __future__ import annotations

import logging

from kestrel.db.engine import connect
from kestrel.services import config as config_service

log = logging.getLogger(__name__)

SOURCES = [
    {
        "key": "eastmoney",
        "label": "东方财富",
        "label_en": "Eastmoney",
        "markets": "A 股 / 港股 / 美股",
        "markets_en": "CN / HK / US",
        "kind": "quote+history",
        "note": "JSON、UTF-8、字段最全，主源。",
        "note_en": "JSON, UTF-8, richest fields — primary source.",
    },
    {
        "key": "tencent",
        "label": "腾讯行情",
        "label_en": "Tencent Finance",
        "markets": "A 股 / 港股 / 美股",
        "markets_en": "CN / HK / US",
        "kind": "quote",
        "note": "GBK 文本，速度快，作备份。",
        "note_en": "Fast quote feed used as primary fallback.",
    },
    {
        "key": "sina",
        "label": "新浪行情",
        "label_en": "Sina Finance",
        "markets": "A 股 / 港股 / 美股",
        "markets_en": "CN / HK / US",
        "kind": "quote",
        "note": "需要 Referer，作第三备份。",
        "note_en": "Tertiary quote fallback with Referer header.",
    },
]

DEFAULTS = {"eastmoney": True, "tencent": True, "sina": True}


def is_enabled(key: str) -> bool:
    conn = connect()
    try:
        value = config_service.get(conn, f"datasource.{key}.enabled", None)
    finally:
        conn.close()
    if value is None:
        return DEFAULTS.get(key, True)
    return bool(value)


def list_sources(locale: str = "zh-CN") -> list[dict]:
    en = str(locale).lower().startswith("en")
    out = []
    for item in SOURCES:
        row = dict(item)
        if en:
            row["label"] = item["label_en"]
            row["markets"] = item["markets_en"]
            row["note"] = item["note_en"]
        row["enabled"] = is_enabled(item["key"])
        out.append(row)
    return out


def build():
    """Construct the provider chain from the current configuration."""
    from kestrel.datasources.chain import QuoteChain
    from kestrel.datasources.custom import CustomProvider, load_custom
    from kestrel.datasources.eastmoney import EastmoneyProvider
    from kestrel.datasources.sina import SinaProvider
    from kestrel.datasources.tencent import TencentProvider

    factories = {
        "eastmoney": EastmoneyProvider,
        "tencent": TencentProvider,
        "sina": SinaProvider,
    }
    providers = []
    # custom sources go first: the user added them for a reason
    try:
        for config in load_custom():
            providers.append(CustomProvider(config))
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot load custom sources: %s", exc)

    for item in SOURCES:
        if not is_enabled(item["key"]):
            continue
        try:
            providers.append(factories[item["key"]]())
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot build source %s: %s", item["key"], exc)
    if not providers:
        providers = [EastmoneyProvider()]
    return QuoteChain(providers=providers)


def list_all(locale: str = "zh-CN") -> list[dict]:
    """Built-ins plus user-defined sources, for the settings table."""
    from kestrel.datasources.custom import load_custom

    en = str(locale).lower().startswith("en")
    out = []
    for row in list_sources(locale):
        row["builtin"] = True
        out.append(row)
    for cfg in load_custom():
        out.append(
            {
                "key": cfg.get("key"),
                "label": cfg.get("label"),
                "markets": cfg.get("markets") or ("All" if en else "全部"),
                "kind": "custom",
                "note": cfg.get("url", ""),
                "enabled": True,
                "builtin": False,
            }
        )
    return out
