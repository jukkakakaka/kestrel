"""原生桌面外壳：围绕本地 FastAPI 服务提供跨平台原生窗口与系统托盘后台常驻。

需要安装 ``desktop`` 可选依赖（pywebview, pystray, pillow, darkdetect）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

from kestrel.desktop.tray import (
    BRAND,
    TrayController,
    load_pil_icon,
    start_tray,
    tray_supported,
)

log = logging.getLogger(__name__)

# 默认桌面工作台窗口尺寸与最小尺寸约束
DEFAULT_WIDTH = 1360
DEFAULT_HEIGHT = 860
MIN_WINDOW_SIZE = (960, 640)


def _icon_image():
    """向后兼容获取 PIL 品牌图标。"""
    return load_pil_icon()


def _start_tray(url: str, on_quit, *, window: Any = None, server: Any = None) -> TrayController | None:
    """启动系统托盘控制器；不支持或显式禁用时返回 ``None``。"""
    return start_tray(url=url, on_quit=on_quit, window=window, server=server)


def run_desktop(url: str, server: Any = None, start_hidden: bool = False) -> None:
    """创建原生窗口并阻塞运行直至用户显式退出应用。

    - 当系统托盘可用且 ``desktop.close_to_tray`` 开启时，点击窗口关闭按钮只会隐藏窗口，
      本地 uvicorn 服务与 APScheduler 盯盘任务继续在后台常驻运行；可通过菜单栏/托盘图标
      或 macOS Dock 图标随时恢复显示。
    - 当 ``start_hidden=True``（例如开机自启 ``kestrel desktop --minimized``）时，
      主窗口初始以隐藏模式在后台启动。
    - 若当前环境无法创建原生 WebView 窗口，则自动回退打开系统默认浏览器并保持服务运行。
    """
    stop = threading.Event()

    def quit_app() -> None:
        if server is not None:
            server.should_exit = True
        stop.set()

    tray = TrayController(url=url, server=server, on_quit=quit_app)

    window_shown = False
    try:
        import webview

        window = webview.create_window(
            "Kestrel",
            url,
            width=DEFAULT_WIDTH,
            height=DEFAULT_HEIGHT,
            min_size=MIN_WINDOW_SIZE,
            hidden=bool(start_hidden),
            background_color="#090d16" if _prefers_dark() else "#f4f6fa",
        )
        tray.attach_window(window)
        if window is not None and hasattr(window, "events"):
            window.events.closing += tray.handle_window_closing

        def _on_webview_ready() -> None:
            tray.start()
            if start_hidden and window is not None and not tray.active:
                # 若要求隐藏启动但当前环境无系统托盘，避免成为不可见孤儿进程，自动恢复显示窗口
                with contextlib.suppress(Exception):
                    window.show()

        webview.start(func=_on_webview_ready)
        window_shown = True
    except Exception as exc:  # noqa: BLE001
        log.warning("native window unavailable: %s", exc)

    if not window_shown:
        # 原生窗口不可用时启动托盘（若支持）并在非隐藏启动时打开默认浏览器兜底
        tray.start()
        if not start_hidden:
            import webbrowser

            log.info("falling back to the browser at %s", url)
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        try:
            while not stop.is_set():
                stop.wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            tray.stop()
            quit_app()
        return

    log.info("desktop window loop exited")

    # 在 Windows / Linux 下若窗口销毁但托盘仍常驻且未请求退出，则阻塞保持后台服务
    if tray.active and not tray.quitting:
        tray.notify("Kestrel 仍在后台盯盘运行，点击托盘图标可重新打开。", "Kestrel")
        try:
            while not stop.is_set() and not tray.quitting:
                stop.wait(3600)
        except KeyboardInterrupt:
            pass

    tray.stop()
    quit_app()


def _prefers_dark() -> bool:
    """探测操作系统当前是否处于深色外观模式。"""
    try:
        import darkdetect

        return bool(darkdetect.isDark())
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "BRAND",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "MIN_WINDOW_SIZE",
    "TrayController",
    "run_desktop",
    "tray_supported",
]
