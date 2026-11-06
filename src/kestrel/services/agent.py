"""Analysis engines: rules (free), LLM (OpenAI compatible), WorkBuddy file bridge.

Agents may only rewrite the explanatory prose. Prices, stops and position sizes are
produced locally by :mod:`kestrel.planning` and are never delegated.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import httpx

from kestrel.db.engine import connect
from kestrel.services import settings_schema
from kestrel.settings import settings

log = logging.getLogger(__name__)

BRIDGE_ROOT = settings.home / "bridge" / "workbuddy"

PROMPT = (
    "You rewrite a stock trade plan for a beginner. Keep every number exactly as given. "
    "Do not add predictions, do not change prices, sizes or stops. Reply in {lang} with "
    "three short sections: what this trade is, why now, what happens if it is wrong. "
    "Maximum 90 words. Plan:\n{plan}"
)


async def enhance(explain: str, locale: str = "zh-CN") -> str:
    """Return a rewritten explanation, or the original when no agent is configured."""
    conn = connect()
    try:
        cfg = settings_schema.read_all(conn)
    finally:
        conn.close()

    mode = cfg.get("agent.mode", "rules")
    if mode == "llm" and cfg.get("agent.llm.enabled"):
        return await _llm(explain, locale, cfg)
    if mode == "workbuddy":
        return await _workbuddy(explain, locale, cfg)
    return explain


async def _llm(explain: str, locale: str, cfg: dict) -> str:
    base = (cfg.get("agent.llm.base_url") or "").rstrip("/")
    key = cfg.get("agent.llm.api_key") or ""
    model = cfg.get("agent.llm.model") or ""
    if not (base and model):
        return explain
    if not _budget_ok(cfg):
        log.info("agent daily budget exhausted, falling back to rules")
        return explain

    lang = "Chinese" if locale.startswith("zh") else "English"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(lang=lang, plan=explain)}],
        "temperature": 0.3,
        "max_tokens": 400,
    }
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"{base}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("llm rewrite failed: %s", exc)
        return explain
    _spend()
    return text or explain


def _budget_ok(cfg: dict) -> bool:
    limit = int(cfg.get("agent.budget_per_day") or 50)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM config_kv WHERE key='agent.spent'"
        ).fetchone()
        spent = json.loads(row["value"]) if row else {"date": "", "count": 0}
    finally:
        conn.close()
    if spent.get("date") != str(date.today()):
        return True
    return spent.get("count", 0) < limit


def _spend() -> None:
    from kestrel.services import config as config_service

    conn = connect()
    try:
        row = conn.execute("SELECT value FROM config_kv WHERE key='agent.spent'").fetchone()
        spent = json.loads(row["value"]) if row else {"date": "", "count": 0}
        if spent.get("date") != str(date.today()):
            spent = {"date": str(date.today()), "count": 0}
        spent["count"] += 1
        config_service.put(conn, "agent.spent", spent)
        conn.commit()
    finally:
        conn.close()


# --- WorkBuddy file bridge ---------------------------------------------

def bridge_paths() -> dict[str, Path]:
    paths = {name: BRIDGE_ROOT / name for name in ("pending", "outbox", "archive", "failed")}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def write_task(task_id: str, kind: str, payload: dict) -> Path:
    paths = bridge_paths()
    target = paths["pending"] / f"{task_id}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"task_id": task_id, "kind": kind, "created_at": date.today().isoformat(),
             "payload": payload},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def read_result(task_id: str) -> dict | None:
    path = bridge_paths()["outbox"] / f"{task_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    shutil.move(str(path), str(bridge_paths()["archive"] / path.name))
    return data


async def _workbuddy(explain: str, locale: str, cfg: dict) -> str:
    """Write a task, then try to use a result produced by a WorkBuddy automation.

    Async and best-effort by design: if no agent picked the task up within the same
    request, the rules text is used and the task stays queued for the next run.
    """
    import uuid

    task_id = f"{date.today().isoformat()}-{uuid.uuid4().hex[:8]}"
    write_task(task_id, "rewrite_explain", {"locale": locale, "explain": explain})
    result = read_result(task_id)
    if result and result.get("text"):
        return result["text"]
    return explain


def bridge_cli(task_id: str) -> int:
    """Reference implementation of the agent side: read a task, print it as JSON."""
    paths = bridge_paths()
    path = paths["pending"] / f"{task_id}.json"
    if not path.exists():
        print(json.dumps({"error": "task not found"}), file=sys.stderr)
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def run_cli(cmd: list[str], timeout: int = 60) -> str:
    """Run an arbitrary CLI agent and return stdout (used by the cli_agent mode)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("cli agent failed: %s", exc)
        return ""
    return out.stdout.strip()


def tempdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="kestrel-"))
