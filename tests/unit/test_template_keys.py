"""模板里引用的每个翻译键都必须存在。

英文页面里漏一个键不会报错——`t()` 会原样返回键名，页面上就出现
`journal.save_failed` 这种字符串。这比崩溃更难发现，所以用测试钉住。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kestrel.i18n import LOCALES_DIR

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "kestrel" / "web" / "templates"

# 这些是拼出来的键（例如 'adv.action_' + action、'strategy.' + code），
# 模板里只会出现前缀，真正成员分别校验。
DYNAMIC_PREFIXES = ("adv.action_", "adv.status_", "strategy.")


def _catalog(locale: str) -> dict[str, str]:
    return json.loads((LOCALES_DIR / f"{locale}.json").read_text(encoding="utf-8"))


def _referenced_keys() -> set[str]:
    pattern = re.compile(r"\bt\('([a-zA-Z0-9_.]+)'")
    keys: set[str] = set()
    for path in TEMPLATES.glob("*.html"):
        keys |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


@pytest.fixture(scope="module")
def keys() -> set[str]:
    return _referenced_keys()


def test_every_template_key_exists_in_both_locales(keys):
    for locale in ("zh-CN", "en"):
        catalog = _catalog(locale)
        missing = sorted(
            key
            for key in keys
            if key not in catalog
            and not key.endswith("_")
            and not key.endswith(".")
            and not any(key.startswith(prefix) for prefix in DYNAMIC_PREFIXES)
        )
        assert not missing, f"{locale} is missing: {missing}"


def test_dynamic_key_families_have_members(keys):
    catalog = _catalog("zh-CN")
    for prefix in DYNAMIC_PREFIXES:
        members = [key for key in catalog if key.startswith(prefix)]
        assert members, f"no keys under {prefix}"


def test_locale_labels_are_written_in_their_own_language():
    from kestrel.i18n import locale_label

    assert locale_label("zh-CN") == "简体中文"
    assert locale_label("en") == "English"


def test_today_briefing_speaks_the_requested_language(tmp_path, monkeypatch):
    from kestrel.db.migrate import init_db
    from kestrel.services import today
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()

    zh = today.briefing("zh-CN")
    en = today.briefing("en")
    assert any("扫描" in item["title"] for item in zh["todos"])
    assert any("scan" in item["title"].lower() for item in en["todos"])
    assert zh["headline"] != en["headline"]
