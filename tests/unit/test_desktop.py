"""The desktop shell must never take AppKit off the main thread.

Creating an NSStatusItem from a Python worker thread traps inside BoardServices
(``-[BSServiceMainRunLoopQueue assertBarrierOnQueue]``) and kills the process
about a second after launch. That is why ``tray_supported()`` is hard-coded to
False on macOS: pywebview already owns the single main thread.
"""

from __future__ import annotations

import sys

import pytest

from kestrel.desktop import app as desktop


def test_no_tray_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("KESTREL_TRAY", raising=False)
    assert desktop.tray_supported() is False


def test_tray_env_switch_disables_anywhere(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("KESTREL_TRAY", "0")
    assert desktop.tray_supported() is False

    monkeypatch.setenv("KESTREL_TRAY", "1")
    assert desktop.tray_supported() is True


def test_start_tray_is_a_no_op_on_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("KESTREL_TRAY", raising=False)
    # Must not import pystray, must not raise, must not start a thread.
    assert desktop._start_tray("http://127.0.0.1:8000", lambda: None) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_falsy_values(monkeypatch, value):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("KESTREL_TRAY", value)
    assert desktop.tray_supported() is False
