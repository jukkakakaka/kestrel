"""Signed short codes for the mobile receipt page."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from kestrel.core.clock import iso, utcnow

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LEN = 8


def _secret() -> bytes:
    from kestrel.settings import settings

    path = settings.secret_key_path
    if not path.exists():
        settings.ensure_dirs()
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


def make_code(rec_id: int) -> str:
    """Deterministic, signed short code for a recommendation."""
    digest = hmac.new(_secret(), f"rec:{rec_id}".encode(), hashlib.sha256).digest()
    num = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(CODE_LEN):
        num, rem = divmod(num, len(ALPHABET))
        chars.append(ALPHABET[rem])
    return "".join(chars)


def verify(code: str) -> bool:
    return bool(code) and len(code) == CODE_LEN and all(c in ALPHABET for c in code)


def expiry(hours: int = 72) -> str:
    return iso(utcnow() + timedelta(hours=hours))
