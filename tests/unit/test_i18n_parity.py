"""Every locale must define exactly the same keys."""

from __future__ import annotations

import json

from kestrel.i18n import LOCALES_DIR, available_locales


def _keys(locale: str) -> set[str]:
    data = json.loads((LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return set(data)


def test_locales_exist():
    locales = available_locales()
    assert "zh-CN" in locales
    assert "en" in locales


def test_parity():
    base = _keys("zh-CN")
    for locale in available_locales():
        assert _keys(locale) == base, f"{locale} keys differ from zh-CN"
