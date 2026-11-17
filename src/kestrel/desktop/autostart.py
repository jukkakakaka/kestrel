"""跨平台用户级开机自启管理（macOS LaunchAgents / Windows Startup / Linux XDG Autostart）。

支持通过环境变量 ``KESTREL_AUTOSTART_DIR`` 覆盖启动项写入目录，确保单元测试
完全隔离在临时目录内，绝不污染真实系统启动目录。
"""

from __future__ import annotations

import logging
import os
import plistlib
import shlex
import sys
from pathlib import Path
from typing import Any

from kestrel.settings import is_frozen

log = logging.getLogger(__name__)

LAUNCH_AGENT_LABEL = "com.kestrel.desktop"
PLIST_FILENAME = f"{LAUNCH_AGENT_LABEL}.plist"
DESKTOP_FILENAME = "kestrel.desktop"
WINDOWS_CMD_FILENAME = "Kestrel.cmd"


def autostart_dir(platform: str | None = None) -> Path:
    """返回用户级开机自启目录；若设置了 ``KESTREL_AUTOSTART_DIR`` 则无条件优先使用。"""
    override = os.environ.get("KESTREL_AUTOSTART_DIR", "").strip()
    if override:
        return Path(override).expanduser()

    plat = platform or sys.platform
    if plat == "darwin":
        return Path.home() / "Library" / "LaunchAgents"
    if plat == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (
            Path(appdata)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg_config) / "autostart"


def autostart_file(platform: str | None = None) -> Path:
    """返回当前平台对应的自启配置文件完整路径。"""
    plat = platform or sys.platform
    base = autostart_dir(plat)
    if plat == "darwin":
        return base / PLIST_FILENAME
    if plat == "win32":
        return base / WINDOWS_CMD_FILENAME
    return base / DESKTOP_FILENAME


def resolve_launch_argv(platform: str | None = None) -> list[str]:
    """计算开机后台静默启动 Kestrel 桌面端的命令行参数列表。"""
    plat = platform or sys.platform
    if is_frozen():
        return [sys.executable, "desktop", "--minimized"]

    exe_path = Path(sys.executable)
    scripts_dir = exe_path.parent
    cli_name = "kestrel.exe" if plat == "win32" else "kestrel"
    cli_candidate = scripts_dir / cli_name
    if cli_candidate.exists():
        return [str(cli_candidate), "desktop", "--minimized"]

    if plat == "win32":
        pythonw = scripts_dir / "pythonw.exe"
        if pythonw.exists():
            return [str(pythonw), "-m", "kestrel.cli", "desktop", "--minimized"]

    return [sys.executable, "-m", "kestrel.cli", "desktop", "--minimized"]


def is_installed(platform: str | None = None) -> bool:
    """检查当前用户的开机自启项文件是否已安装。"""
    try:
        return autostart_file(platform).is_file()
    except OSError:
        return False


# 兼容语义化别名
autostart_installed = is_installed


def _render_macos_plist(argv: list[str]) -> bytes:
    """生成 macOS LaunchAgent plist 二进制/XML 字节流。"""
    payload: dict[str, Any] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(payload)


def _render_linux_desktop(argv: list[str]) -> str:
    """生成符合 XDG Autostart 规范的 .desktop 内容。"""
    exec_line = " ".join(shlex.quote(part) for part in argv)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.0\n"
        "Name=Kestrel\n"
        "Comment=Kestrel trading companion background watch\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _render_windows_cmd(argv: list[str]) -> str:
    """生成 Windows 用户 Startup 目录下的无黑框后台启动脚本。"""
    quoted = " ".join(f'"{part}"' for part in argv)
    return f'@echo off\r\nstart "" /b {quoted}\r\n'


def enable_autostart(platform: str | None = None) -> Path:
    """写入当前平台的开机自启配置并返回目标文件路径。"""
    plat = platform or sys.platform
    target = autostart_file(plat)
    target.parent.mkdir(parents=True, exist_ok=True)
    argv = resolve_launch_argv(plat)

    if plat == "darwin":
        target.write_bytes(_render_macos_plist(argv))
    elif plat == "win32":
        target.write_text(_render_windows_cmd(argv), encoding="utf-8")
    else:
        target.write_text(_render_linux_desktop(argv), encoding="utf-8")

    log.info("autostart enabled at %s", target)
    return target


def disable_autostart(platform: str | None = None) -> bool:
    """移除当前平台的开机自启配置；若原本存在并成功删除则返回 True。"""
    target = autostart_file(platform)
    if target.exists():
        target.unlink()
        log.info("autostart removed from %s", target)
        return True
    return False


def sync_autostart(enabled: bool, platform: str | None = None) -> dict[str, Any]:
    """将操作系统自启项同步到目标开关状态，永不抛出未捕获的 OSError。"""
    plat = platform or sys.platform
    target = autostart_file(plat)
    want = bool(enabled)
    try:
        if want:
            enable_autostart(plat)
        else:
            disable_autostart(plat)
        return {
            "ok": True,
            "enabled": want,
            "installed": is_installed(plat),
            "path": str(target),
        }
    except OSError as exc:
        log.warning("failed to sync autostart (%s): %s", target, exc)
        return {
            "ok": False,
            "enabled": want,
            "installed": is_installed(plat),
            "path": str(target),
            "error": str(exc),
        }
