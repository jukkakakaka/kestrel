"""版本比较与更新检查的离线部分。

网络测试不可靠也不该拖慢 CI，所以这里只测纯逻辑：
版本比较、按平台挑安装包、缓存过期判断、容器里拒绝自动安装。
"""

from __future__ import annotations

import os
import sys

import pytest

from kestrel.services import updates


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("0.3.0", "0.2.0", True),
        ("0.2.1", "0.2.0", True),
        ("0.2.0", "0.2.0", False),
        ("0.1.9", "0.2.0", False),
        ("v0.3.0", "0.2.0", True),
        ("1.0.0", "0.9.9", True),
        ("0.3.0-beta.1", "0.2.0", True),   # 解析出版本号后才比较
        ("latest", "0.2.0", False),        # 不是版本号就当没有更新
        ("", "0.2.0", False),
    ],
)
def test_version_comparison(candidate, current, expected):
    assert updates.is_newer(candidate, current) is expected


def _asset(name: str) -> dict:
    return {"name": name, "size": 10, "browser_download_url": f"https://example.com/{name}"}


def _release(names: list[str]) -> updates.Release:
    return updates.Release(
        version="0.3.0", tag="v0.3.0", url="https://example.com", notes="",
        published_at="", assets=[_asset(n) for n in names],
    )


def test_macos_picks_the_dmg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    asset = _release(["Kestrel-windows.zip", "Kestrel-0.3.0.dmg"]).asset_for_platform()
    assert asset["name"].endswith(".dmg")


def test_windows_picks_the_zip(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    asset = _release(["Kestrel-0.3.0.dmg", "Kestrel-windows.zip"]).asset_for_platform()
    assert asset["name"].endswith(".zip")


def test_a_release_without_our_platform_returns_none(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _release(["Kestrel-windows.zip"]).asset_for_platform() is None


def test_mode_labels(monkeypatch):
    monkeypatch.delenv("KESTREL_IN_CONTAINER", raising=False)
    monkeypatch.setattr(updates, "is_frozen", lambda: False)
    assert updates.mode_label() == "source"

    monkeypatch.setattr(updates, "is_frozen", lambda: True)
    assert updates.mode_label() == "app"

    monkeypatch.setenv("KESTREL_IN_CONTAINER", "1")
    assert updates.mode_label() == "docker"


def test_no_platform_label_is_empty():
    assert updates.platform_label()


def test_info_reports_the_running_version():
    info = updates.info()
    assert info["current"] == updates.__version__
    assert info["releases_url"].startswith("https://github.com/")


def test_should_check_respects_the_interval():
    assert updates.should_check(None) is True
    assert updates.should_check({"checked_at": "not a date"}) is True
    fresh = {"checked_at": updates.datetime.now(updates.UTC).isoformat()}
    assert updates.should_check(fresh) is False


@pytest.mark.asyncio
async def test_install_in_a_container_refuses_and_explains(monkeypatch):
    monkeypatch.setenv("KESTREL_IN_CONTAINER", "1")
    result = await updates.install()
    assert result["ok"] is False
    assert "docker compose pull" in result["error"]


@pytest.mark.asyncio
async def test_download_rejects_an_asset_without_a_url():
    result = await updates.download({"name": "x.dmg"})
    assert result["ok"] is False
