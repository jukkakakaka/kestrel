"""Delivery channels.

Adding a channel means implementing ``send(message)`` and registering it in
``CHANNELS``. Everything is optional: a channel that is not configured is skipped.
"""

from __future__ import annotations

import base64
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Message:
    title: str
    body: str
    url: str = ""
    priority: str = "default"


class ChannelError(RuntimeError):
    pass


class NtfyChannel:
    """ntfy.sh or a self-hosted instance. Supports one-tap action buttons."""

    code = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str = "") -> None:
        self.topic = topic
        self.server = server.rstrip("/")
        self.token = token

    async def send(self, message: Message) -> None:
        headers = {"Title": _ascii(message.title), "Priority": message.priority}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if message.url:
            buttons = f"view, 打开回执, {message.url}, clear=true"
            headers["Actions"] = buttons
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.server}/{self.topic}",
                content=message.body.encode("utf-8"),
                headers=headers,
            )
            if resp.status_code >= 300:
                raise ChannelError(f"ntfy {resp.status_code}: {resp.text[:120]}")


class WebhookChannel:
    """Generic JSON webhook — works with WeCom bots, Slack proxies and custom glue."""

    code = "webhook"

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token

    async def send(self, message: Message) -> None:
        payload = {
            "msgtype": "text",
            "text": {"content": f"{message.title}\n{message.body}\n{message.url}".strip()},
            "title": message.title,
            "body": message.body,
            "url": message.url,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(self.url, json=payload)
            if resp.status_code >= 300:
                raise ChannelError(f"webhook {resp.status_code}: {resp.text[:120]}")


class WeClawBotChannel:
    """WeChat push through a WeClawBot-API gateway (personal WeChat, push only).

    The upstream project is in grey release, so failures here must never block a
    scan — the dispatcher downgrades to the next channel.
    """

    code = "weclawbot"

    def __init__(self, base_url: str, bot_id: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.bot_id = bot_id
        self.api_token = api_token

    async def send(self, message: Message) -> None:
        text = f"{message.title}\n{message.body}"
        if message.url:
            text += f"\n{message.url}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{self.base_url}/bots/{self.bot_id}/messages",
                json={"text": text},
                headers={"Authorization": f"Bearer {self.api_token}"},
            )
            if resp.status_code >= 300:
                raise ChannelError(f"weclawbot {resp.status_code}: {resp.text[:120]}")


class SmtpChannel:
    code = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str, sender: str, to: str) -> None:
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.sender, self.to = sender, to

    async def send(self, message: Message) -> None:
        mail = EmailMessage()
        mail["Subject"] = message.title
        mail["From"] = self.sender
        mail["To"] = self.to
        mail.set_content(f"{message.body}\n\n{message.url}".strip())
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(mail)


class WorkBuddyChannel:
    """Hand messages to WorkBuddy through a shared outbox folder.

    There is no inbound webhook for an in-app WeChat bot, so the daemon writes a
    JSON file and a WorkBuddy automation picks it up. The file *is* the channel:
    if the automation is missing the message simply sits there, which is a much
    better failure mode than a silent drop.
    """

    code = "workbuddy"

    def __init__(self, outbox: str) -> None:
        self.outbox = outbox

    async def send(self, message: Message) -> None:
        import json
        import uuid
        from datetime import datetime
        from pathlib import Path

        folder = Path(self.outbox).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{uuid.uuid4().hex[:6]}.json"
        payload = {
            "title": message.title,
            "body": message.body,
            "url": message.url,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        (folder / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")


def _ascii(text: str) -> str:
    """ntfy headers must be ASCII; fall back to RFC 2047 when needed."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode() + "?="


def build_channel(code: str, cfg: dict):
    if code == "ntfy":
        return NtfyChannel(cfg.get("topic", ""), cfg.get("server", "https://ntfy.sh"), cfg.get("token", ""))
    if code == "webhook":
        return WebhookChannel(cfg.get("url", ""), cfg.get("token", ""))
    if code == "weclawbot":
        return WeClawBotChannel(cfg.get("base_url", ""), cfg.get("bot_id", ""), cfg.get("api_token", ""))
    if code == "workbuddy":
        return WorkBuddyChannel(cfg.get("outbox", ""))
    if code == "smtp":
        return SmtpChannel(
            cfg.get("host", ""), cfg.get("port", 587), cfg.get("user", ""),
            cfg.get("password", ""), cfg.get("sender", ""), cfg.get("to", ""),
        )
    return None
