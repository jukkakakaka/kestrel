"""Notification channel configuration, stored as data not code.

Each channel has a small field schema so the settings page can render a form
without knowing what ntfy or SMTP need.
"""

from __future__ import annotations

import json
from typing import Any

from kestrel.db.engine import connect

SCHEMA: dict[str, dict[str, Any]] = {
    "workbuddy": {
        "label": "WorkBuddy（微信 clawbot）",
        "hint": "写文件到 outbox，由 WorkBuddy 定时任务转发到微信。不需要填接口地址。",
        "fields": [
            {"key": "outbox", "label": "outbox 目录", "kind": "text",
             "placeholder": "~/.workbuddy/kestrel-outbox"},
        ],
    },
    "weclawbot": {
        "label": "WeClawBot 网关",
        "hint": "自建 WeClawBot-API 的情况填这里：base_url + bot_id + token。",
        "fields": [
            {"key": "base_url", "label": "网关地址", "kind": "text",
             "placeholder": "http://127.0.0.1:8848"},
            {"key": "bot_id", "label": "bot id", "kind": "text", "placeholder": "bot1"},
            {"key": "api_token", "label": "Token", "kind": "password", "placeholder": ""},
        ],
    },
    "ntfy": {
        "label": "ntfy",
        "hint": "手机装 ntfy App，订阅一个 topic 就能收到，免费且支持一键回执按钮。",
        "fields": [
            {"key": "server", "label": "服务器", "kind": "text", "placeholder": "https://ntfy.sh"},
            {"key": "topic", "label": "Topic", "kind": "text", "placeholder": "kestrel-yourname"},
            {"key": "token", "label": "Token（可选）", "kind": "password", "placeholder": ""},
        ],
    },
    "webhook": {
        "label": "通用 Webhook",
        "hint": "企业微信群机器人、Slack 中转、自建服务都行。",
        "fields": [
            {"key": "url", "label": "Webhook 地址", "kind": "text", "placeholder": "https://..."},
            {"key": "token", "label": "Token（可选）", "kind": "password", "placeholder": ""},
        ],
    },
    "smtp": {
        "label": "邮件",
        "hint": "适合每天收盘后收一封汇总。",
        "fields": [
            {"key": "host", "label": "SMTP 服务器", "kind": "text", "placeholder": "smtp.gmail.com"},
            {"key": "port", "label": "端口", "kind": "number", "placeholder": "587"},
            {"key": "user", "label": "账号", "kind": "text", "placeholder": ""},
            {"key": "password", "label": "密码 / 授权码", "kind": "password", "placeholder": ""},
            {"key": "sender", "label": "发件人", "kind": "text", "placeholder": ""},
            {"key": "to", "label": "收件人", "kind": "text", "placeholder": ""},
        ],
    },
}


def _rows() -> dict[str, dict]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM notify_channel").fetchall()
    finally:
        conn.close()
    out = {}
    for row in rows:
        item = dict(row)
        try:
            item["config"] = json.loads(item["config"]) if item["config"] else {}
        except (TypeError, ValueError):
            item["config"] = {}
        out[item["key"]] = item
    return out


def all_channels() -> list[dict]:
    stored = _rows()
    out = []
    for key, meta in SCHEMA.items():
        row = stored.get(key, {})
        out.append(
            {
                "key": key,
                "label": meta["label"],
                "hint": meta["hint"],
                "fields": meta["fields"],
                "enabled": bool(row.get("enabled")),
                "config": row.get("config") or {},
                "last_test": row.get("last_test"),
                "last_error": row.get("last_error"),
            }
        )
    return out


def save(key: str, payload: dict) -> None:
    meta = SCHEMA.get(key)
    if meta is None:
        return
    values = {}
    for field in meta["fields"]:
        name = field["key"]
        if name in payload:
            values[name] = payload.get(name)
    enabled = bool(payload.get("enabled"))
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO notify_channel (key, label, kind, enabled, config, updated_at)
            VALUES (?,?,?,?,?,datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                enabled=excluded.enabled, config=excluded.config, updated_at=datetime('now')
            """,
            (key, meta["label"], key, int(enabled), json.dumps(values, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def config_for(key: str) -> dict:
    row = _rows().get(key)
    if not row:
        return {}
    return row.get("config") or {}


def enabled_channels() -> list[tuple[str, dict]]:
    stored = _rows()
    out = []
    for key in SCHEMA:
        row = stored.get(key)
        if not row or not row.get("enabled"):
            continue
        conf = row.get("config") or {}
        # a channel with no address at all is "on" but useless; skip it
        if key == "ntfy" and not conf.get("topic"):
            continue
        if key == "webhook" and not conf.get("url"):
            continue
        if key == "weclawbot" and not (conf.get("base_url") and conf.get("bot_id")):
            continue
        if key == "workbuddy" and not conf.get("outbox"):
            continue
        if key == "smtp" and not (conf.get("host") and conf.get("to")):
            continue
        out.append((key, conf))
    return out


def mark_result(key: str, ok: bool, error: str = "") -> None:
    from datetime import datetime

    conn = connect()
    try:
        conn.execute(
            "UPDATE notify_channel SET last_test=?, last_error=? WHERE key=?",
            (
                datetime.now().isoformat(timespec="seconds") if ok else None,
                None if ok else error[:300],
                key,
            ),
        )
        conn.commit()
    finally:
        conn.close()
