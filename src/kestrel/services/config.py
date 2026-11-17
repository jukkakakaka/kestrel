"""Small typed wrapper over the ``config_kv`` table."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

DEFAULTS: dict[str, Any] = {
    "account.capital": 50000.0,
    "account.currency": "CNY",
    "risk.per_trade_pct": 2.0,
    "risk.max_total_exposure_pct": 60.0,
    "risk.max_per_sector": 2,
    "risk.daily_loss_limit_pct": 5.0,
    "scan.auto_enabled": False,
    "scan.interval_minutes": 60,
    "trade.horizon": "auto",
    "scan.markets": ["CN", "HK", "US"],
    "ui.markets_configured": False,
    "scan.min_score": 0.55,
    "scan.universe": "both",
    "scan.pool_size": 30,
    "ui.learning_mode": True,
    "ui.glossary": True,
    "broker.HK": "fosun_hk",
    "broker.US": "fosun_us",
    "broker.CN": "cn_generic",
    "notify.channels": [],
    "notify.locale": "zh-CN",
    "receipt.public_base": "",
    "agent.provider": "rules",
    "agent.base_url": "",
    "agent.model": "",
    "agent.api_key": "",
    "agent.daily_budget": 200,
    "update.auto_check": True,
    "update.auto_install": False,
    "ui.display_currency": "local",
    "ui.privacy": False,
    "desktop.close_to_tray": True,
    "desktop.autostart": False,
    "bridge.root": "",
}

# 布尔配置项集合，统一在读写时规范化为严格 bool 类型
_BOOL_KEYS: frozenset[str] = frozenset(
    {
        "ui.privacy",
        "desktop.close_to_tray",
        "desktop.autostart",
    }
)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """将任意输入值安全转换为布尔值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in ("1", "true", "yes", "on"):
            return True
        if norm in ("0", "false", "no", "off", ""):
            return False
    if value is None:
        return default
    return bool(value)


def _normalize(key: str, value: Any) -> Any:
    """对声明了类型约束的配置项进行读写校验与归一化。"""
    if key in _BOOL_KEYS:
        return _coerce_bool(value, bool(DEFAULTS.get(key, False)))
    return value


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM config_kv WHERE key=?", (key,)).fetchone()
    if row is None:
        fallback = DEFAULTS.get(key, default)
        return _normalize(key, fallback) if key in _BOOL_KEYS else fallback
    try:
        parsed = json.loads(row["value"])
    except json.JSONDecodeError:
        fallback = DEFAULTS.get(key, default)
        return _normalize(key, fallback) if key in _BOOL_KEYS else fallback
    return _normalize(key, parsed)


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    norm_val = _normalize(key, value)
    conn.execute(
        """
        INSERT INTO config_kv (key, value, updated_at) VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, json.dumps(norm_val, ensure_ascii=False)),
    )


def all_values(conn: sqlite3.Connection) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM config_kv").fetchall():
        try:
            key = row["key"]
            merged[key] = _normalize(key, json.loads(row["value"]))
        except json.JSONDecodeError:
            continue
    return merged
