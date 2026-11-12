"""A file queue between this app and a WorkBuddy agent.

There is no local HTTP endpoint to call, so the app writes a task as a JSON file
and waits for the agent to write an answer next to it. That inverts the usual
request/response: analysis becomes asynchronous, which is honest — a real model
takes tens of seconds and nobody should hold an HTTP connection for it.

Layout under ``<data>/bridge``::

    pending/<id>.json    request written by the app
    done/<id>.json       answer written by the agent
    failed/<id>.json     answer with an error, kept for inspection
    archive/             collected requests, so nothing is replayed twice

The agent side only has to read ``pending/*.json``, write ``done/<same-id>.json``
with ``{"picks": [...]}`` and move on. See :func:`task_prompt`.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from kestrel.db.engine import connect
from kestrel.settings import settings

log = logging.getLogger(__name__)

# 旧常量保留给仍按模块级路径读取的调用方（例如脚本）；实际写入一律走 paths()，
# 因为 bridge.root 可以在设置里改。
BRIDGE = settings.home / "bridge"
PENDING = BRIDGE / "pending"
DONE = BRIDGE / "done"
FAILED = BRIDGE / "failed"
ARCHIVE = BRIDGE / "archive"


def root() -> Path:
    """桥目录。``bridge.root`` 为空时跟随数据目录。

    浏览器版、打包版和容器版的数据目录完全不同，WorkBuddy 的定时任务
    跑在应用外面，所以这个目录必须能被用户显式指向一个共享位置——
    否则容器里的 `/data/bridge` 外面根本看不到。
    """
    from kestrel.services import config as config_service

    conn = connect()
    try:
        configured = str(config_service.get(conn, "bridge.root", "") or "").strip()
    finally:
        conn.close()
    if not configured:
        return settings.home / "bridge"
    return Path(configured).expanduser()


def paths() -> dict[str, Path]:
    base = root()
    return {
        "pending": base / "pending",
        "done": base / "done",
        "failed": base / "failed",
        "archive": base / "archive",
    }


def ensure_dirs() -> None:
    for folder in paths().values():
        folder.mkdir(parents=True, exist_ok=True)


def new_id(kind: str = "analyse") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:6]}"


def queue(kind: str, prompt: str, payload: dict, meta: dict | None = None) -> str:
    """Write a request the agent can pick up. Returns the request id."""
    ensure_dirs()
    folders = paths()
    request_id = new_id(kind)
    body = {
        "id": request_id,
        "kind": kind,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reply_to": str(folders["done"] / f"{request_id}.json"),
        "instructions": prompt,
        "input": payload,
        "output_contract": (
            'Write done/<id>.json with exactly: {"picks": [...], "market_view": "...", '
            '"pass_reason": "..."} — same shape the instructions describe. '
            "Do not wrap it in Markdown."
        ),
        **(meta or {}),
    }
    (folders["pending"] / f"{request_id}.json").write_text(
        json.dumps(body, ensure_ascii=False, indent=2), "utf-8"
    )
    log.info("bridge queued %s", request_id)
    return request_id


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("bridge cannot read %s: %s", path.name, exc)
        return None


def collect(kind: str | None = None, limit: int = 20) -> list[tuple[str, dict]]:
    """Answers the agent has written since we last looked."""
    ensure_dirs()
    folders = paths()
    out: list[tuple[str, dict]] = []
    for path in sorted(folders["done"].glob("*.json")):
        if kind and not path.stem.startswith(kind):
            continue
        body = _read(path)
        if body is None:
            path.rename(folders["failed"] / path.name)
            continue
        # carry the original request along so the caller still has the brief
        request = _read(folders["pending"] / path.name) or {}
        if request:
            body["_request"] = request
        out.append((path.stem, body))
        path.rename(folders["archive"] / path.name)
        pending = folders["pending"] / path.name
        if pending.exists():
            pending.rename(folders["archive"] / pending.name)
        if len(out) >= limit:
            break
    return out


def pending_requests(kind: str | None = None) -> list[dict]:
    ensure_dirs()
    rows = []
    for path in sorted(paths()["pending"].glob("*.json")):
        if kind and not path.stem.startswith(kind):
            continue
        body = _read(path)
        if body:
            rows.append(body)
    return rows


def pending_count() -> int:
    ensure_dirs()
    return len(list(paths()["pending"].glob("*.json")))


def answer(request_id: str, body: dict) -> bool:
    """Used by the agent side (and by tests) to post a result."""
    ensure_dirs()
    target = paths()["done"] / f"{request_id}.json"
    target.write_text(json.dumps(body, ensure_ascii=False, indent=2), "utf-8")
    return True


def task_prompt(request: dict) -> str:
    """The exact text an agent should read. Also shown in the UI."""
    return (
        f"{request.get('instructions', '')}\n\n"
        "---\n"
        f"请求编号：{request.get('id')}\n"
        f"处理完成后把 JSON 写到：{request.get('reply_to')}\n"
        "格式：{\"picks\": [...], \"market_view\": \"...\", \"pass_reason\": \"...\"}\n"
        "不要输出 Markdown，不要输出解释文字。\n"
    )


def bridge_info() -> dict:
    folders = paths()
    return {
        "root": str(root()),
        "pending_dir": str(folders["pending"]),
        "done_dir": str(folders["done"]),
        "pending": pending_count(),
    }
