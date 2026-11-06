"""Minimal JSON catalogue i18n. Keys must exist in every locale (enforced by tests)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LOCALE = "zh-CN"


def available_locales() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


@lru_cache(maxsize=8)
def load_catalog(locale: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{locale}.json"
    if not path.exists():
        path = LOCALES_DIR / f"{DEFAULT_LOCALE}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def translate(locale: str, key: str, **kwargs) -> str:
    catalog = load_catalog(locale)
    text = catalog.get(key) or load_catalog(DEFAULT_LOCALE).get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text
