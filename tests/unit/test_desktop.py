"""桌面外壳与跨平台系统托盘支持检测单元测试。

macOS 通过 PyObjC ``AppKit`` + ``PyObjCTools.AppHelper.callAfter`` 在主线程安全创建
``NSStatusItem``；Windows / Linux 通过 ``pystray`` 守护线程创建托盘；任意平台均可
通过 ``KESTREL_TRAY=0`` 显式禁用托盘。
"""

from __future__ import annotations

import sys
import types

import pytest

from kestrel.desktop import app as desktop
from kestrel.desktop import tray


def test_macos_tray_uses_appkit_on_main_thread(monkeypatch):
    """macOS 在 AppKit 可用且未禁用时支持原生托盘，并通过 AppHelper.callAfter 投递主线程。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("KESTREL_TRAY", raising=False)
    monkeypatch.setattr(tray, "_macos_appkit_available", lambda: True)
    assert desktop.tray_supported() is True

    scheduled: list = []
    fake_helper = types.ModuleType("PyObjCTools.AppHelper")
    fake_helper.callAfter = lambda fn, *a, **kw: scheduled.append(fn)  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("PyObjCTools")
    fake_pkg.AppHelper = fake_helper  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyObjCTools", fake_pkg)
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", fake_helper)

    ctrl = desktop._start_tray("http://127.0.0.1:8000", lambda: None)
    assert ctrl is not None
    assert ctrl.active is True
    assert len(scheduled) == 1


def test_macos_tray_unavailable_without_appkit(monkeypatch):
    """当精简环境未安装 PyObjC AppKit 时，macOS 优雅降级返回 False。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("KESTREL_TRAY", raising=False)
    monkeypatch.setattr(tray, "_macos_appkit_available", lambda: False)
    assert desktop.tray_supported() is False
    assert desktop._start_tray("http://127.0.0.1:8000", lambda: None) is None


def test_tray_env_switch_disables_anywhere(monkeypatch):
    """KESTREL_TRAY=0 在任何平台均强制禁用托盘。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(tray, "_pystray_available", lambda: True)
    monkeypatch.setenv("KESTREL_TRAY", "0")
    assert desktop.tray_supported() is False
    assert desktop._start_tray("http://127.0.0.1:8000", lambda: None) is None

    monkeypatch.setenv("KESTREL_TRAY", "1")
    assert desktop.tray_supported() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_falsy_values(monkeypatch, value):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(tray, "_pystray_available", lambda: True)
    monkeypatch.setenv("KESTREL_TRAY", value)
    assert desktop.tray_supported() is False
