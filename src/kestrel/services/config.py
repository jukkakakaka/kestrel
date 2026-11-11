"""Small typed wrapper over the ``config_kv`` table."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

DEFAULTS: dict[str, Any] = {
    "account.capital": 10000.0,
    "account.currency": "CNY",
    "risk.per_trade_pct": 2.0,
    "risk.max_total_exposure_pct": 60.0,
    "risk.max_per_sector": 2,
    "scan.auto_enabled": False,
    "scan.interval_minutes": 60,
    "trade.horizon": "auto",
    "scan.markets": ["CN", "HK", "US"],
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
    # 更新：默认每天自动检查一次，但不自动安装——安装需要用户确认，
    # 否则应用会在用户不知情时被替换掉。
    "update.auto_check": True,
    "update.auto_install": False,
}


def get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM config_kv WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key, default)
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return DEFAULTS.get(key, default)


def put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO config_kv (key, value, updated_at) VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False)),
    )


def all_values(conn: sqlite3.Connection) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for row in conn.execute("SELECT key, value FROM config_kv").fetchall():
        try:
            merged[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            continue
    return merged
