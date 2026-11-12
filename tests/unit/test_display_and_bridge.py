"""价格显示与桥目录：两处都曾经「看起来对，实际误导」。

- 行情页只给数字，同一屏里 HK$409.600 和 US$182.40 长得一样；
- WorkBuddy 的提示词里写死 `data/bridge`，打包版和容器版根本对不上。
"""

from __future__ import annotations

import pytest

from kestrel.app import _quote
from kestrel.db.migrate import init_db
from kestrel.services import bridge


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from kestrel.settings import settings

    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    return tmp_path


def test_native_currency_is_labelled():
    # 100.25 港元
    assert _quote(100_250_000, "HK") == "HK$100.250"
    assert _quote(182_400_000, "US") == "US$182.40"
    assert _quote(1_450_000_000, "CN") == "¥1,450.00"


def test_a_unified_currency_converts_everything():
    usd = _quote(100_000_000, "US", "CNY")
    assert usd.startswith("¥")
    # 100 美元 × 7.1 = 710 人民币
    assert "710" in usd

    hkd = _quote(100_000_000, "HK", "USD")
    assert hkd.startswith("US$")
    # 100 港元 × 0.92 / 7.1 ≈ 12.96
    assert "12.9" in hkd


def test_an_unknown_target_falls_back_to_native():
    assert _quote(100_000_000, "HK", "XXX") == "HK$100.000"


def test_missing_prices_render_as_a_dash():
    assert _quote(None, "HK") == "—"
    assert _quote("", "US") == "—"


def test_the_bridge_follows_the_data_directory_by_default(home):
    assert bridge.paths()["pending"] == home / "bridge" / "pending"


def test_the_bridge_root_can_be_pointed_elsewhere(home, tmp_path):
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service

    shared = tmp_path / "shared-bridge"
    conn = connect()
    try:
        config_service.put(conn, "bridge.root", str(shared))
        conn.commit()
    finally:
        conn.close()

    assert bridge.paths()["done"] == shared / "done"
    bridge.ensure_dirs()
    assert (shared / "pending").is_dir()
    assert (shared / "archive").is_dir()
    info = bridge.bridge_info()
    assert info["root"] == str(shared)


def test_the_generated_prompt_uses_the_configured_root(home, tmp_path):
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service
    from kestrel.services import prompts

    shared = tmp_path / "queue"
    conn = connect()
    try:
        config_service.put(conn, "bridge.root", str(shared))
        conn.commit()
    finally:
        conn.close()

    text = prompts.analyse_prompt()
    assert str(shared / "pending") in text
    assert str(shared / "done") in text
