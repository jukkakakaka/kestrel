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

from kestrel.settings import settings

log = logging.getLogger(__name__)

BRIDGE = settings.home / "bridge"
PENDING = BRIDGE / "pending"
DONE = BRIDGE / "done"
FAILED = BRIDGE / "failed"
ARCHIVE = BRIDGE / "archive"


def ensure_dirs() -> None:
    for folder in (PENDING, DONE, FAILED, ARCHIVE):
        folder.mkdir(parents=True, exist_ok=True)


def new_id(kind: str = "analyse") -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{kind}-{stamp}-{uuid.uuid4().hex[:6]}"


def queue(kind: str, prompt: str, payload: dict, meta: dict | None = None) -> str:
    """Write a request the agent can pick up. Returns the request id."""
    ensure_dirs()
    request_id = new_id(kind)
    body = {
        "id": request_id,
        "kind": kind,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reply_to": str(DONE / f"{request_id}.json"),
        "instructions": prompt,
        "input": payload,
        "output_contract": (
            'Write done/<id>.json with exactly: {"picks": [...], "market_view": "...", '
            '"pass_reason": "..."} — same shape the instructions describe. '
            "Do not wrap it in Markdown."
        ),
        **(meta or {}),
    }
    (PENDING / f"{request_id}.json").write_text(
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
    out: list[tuple[str, dict]] = []
    for path in sorted(DONE.glob("*.json")):
        if kind and not path.stem.startswith(kind):
            continue
        body = _read(path)
        if body is None:
            path.rename(FAILED / path.name)
            continue
        # carry the original request along so the caller still has the brief
        request = _read(PENDING / path.name) or {}
        if request:
            body["_request"] = request
        out.append((path.stem, body))
        path.rename(ARCHIVE / path.name)
        pending = PENDING / path.name
        if pending.exists():
            pending.rename(ARCHIVE / pending.name)
        if len(out) >= limit:
            break
    return out


def pending_requests(kind: str | None = None) -> list[dict]:
    ensure_dirs()
    rows = []
    for path in sorted(PENDING.glob("*.json")):
        if kind and not path.stem.startswith(kind):
            continue
        body = _read(path)
        if body:
            rows.append(body)
    return rows


def pending_count() -> int:
    ensure_dirs()
    return len(list(PENDING.glob("*.json")))


def answer(request_id: str, body: dict) -> bool:
    """Used by the agent side (and by tests) to post a result."""
    ensure_dirs()
    target = DONE / f"{request_id}.json"
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
    return {
        "root": str(BRIDGE),
        "pending_dir": str(PENDING),
        "done_dir": str(DONE),
        "pending": pending_count(),
    }
