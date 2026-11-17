"""跨平台系统托盘与后台常驻控制器。

架构说明：
- macOS 原生后端：``pywebview`` 在主线程运行 Cocoa ``NSApplication`` 事件循环。
  通过 ``PyObjCTools.AppHelper.callAfter`` 将 ``NSStatusItem`` 与 ``NSMenu`` 的创建
  安全投递到 Cocoa 主线程执行，彻底解决 ``pystray`` 在子线程创建状态栏图标触发
  ``BoardServices`` 主线程断言崩溃的问题。
- Windows / Linux 后端：使用 ``pystray`` 守护线程提供同等菜单能力。
- 环境变量 ``KESTREL_TRAY=0`` 可显式禁用托盘（用于无头测试与构建冒烟校验）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from kestrel.settings import bundle_dir

log = logging.getLogger(__name__)

BRAND = bundle_dir() / "assets" / "brand"
_FALSY = ("0", "false", "no", "off")

# 双语菜单文案表
MENU_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "open_window": "打开 Kestrel 主窗口",
        "open_browser": "在浏览器中打开",
        "status_running": "● 后台盯盘运行中",
        "scan_now": "立即扫描市场",
        "toggle_privacy": "切换金额隐私打码",
        "close_to_tray": "关闭窗口时驻留后台",
        "quit": "退出 Kestrel",
    },
    "en": {
        "open_window": "Open Kestrel Window",
        "open_browser": "Open in Browser",
        "status_running": "● Background Watch Active",
        "scan_now": "Scan Markets Now",
        "toggle_privacy": "Mask Monetary Amounts",
        "close_to_tray": "Keep Running on Window Close",
        "quit": "Quit Kestrel",
    },
}

_OBJC_TRAY_DELEGATE_CLS: Any = None
_OBJC_APP_PROXY_CLS: Any = None


def _macos_appkit_available() -> bool:
    """检测当前环境是否具备 PyObjC AppKit 与 AppHelper 支持。"""
    try:
        import AppKit  # noqa: F401
        from PyObjCTools import AppHelper  # noqa: F401

        return True
    except ImportError:
        return False


def _pystray_available() -> bool:
    """检测当前环境是否可导入 pystray。"""
    try:
        import pystray  # noqa: F401

        return True
    except ImportError:
        return False


def tray_supported() -> bool:
    """判断当前运行环境是否支持创建系统托盘/菜单栏图标。"""
    flag = os.environ.get("KESTREL_TRAY", "").strip().lower()
    if flag in _FALSY:
        return False
    if sys.platform == "darwin":
        return _macos_appkit_available()
    return _pystray_available()


def find_brand_icon_path() -> Path | None:
    """查找最适合系统托盘缩放的品牌图标文件路径。"""
    for candidate in (
        BRAND / "favicon" / "favicon-64.png",
        BRAND / "favicon" / "favicon-180.png",
        BRAND / "png" / "logo-mark-512.png",
    ):
        if candidate.exists():
            return candidate
    return None


def load_pil_icon():
    """为 pystray 加载 PIL 图标对象，缺失时生成兜底色块图标。"""
    from PIL import Image

    icon_path = find_brand_icon_path()
    if icon_path is not None:
        return Image.open(icon_path)
    return Image.new("RGBA", (64, 64), (22, 48, 92, 255))


def _detect_locale() -> str:
    """读取用户首选语言（支持 zh-CN / en）。"""
    try:
        from kestrel.db.engine import connect
        from kestrel.services import config as config_service

        conn = connect()
        try:
            raw = str(
                config_service.get(conn, "ui.locale")
                or config_service.get(conn, "notify.locale", "zh-CN")
                or "zh-CN"
            )
        finally:
            conn.close()
        if raw.lower().startswith("en"):
            return "en"
        return "zh-CN"
    except Exception:  # noqa: BLE001
        lang = os.environ.get("LANG", "").lower()
        return "en" if lang.startswith("en") else "zh-CN"


def menu_labels(locale: str | None = None) -> dict[str, str]:
    """返回指定语言的托盘菜单文案字典。"""
    chosen = locale or _detect_locale()
    return MENU_LABELS["en" if str(chosen).lower().startswith("en") else "zh-CN"]


def _read_bool_config(key: str, default: bool) -> bool:
    """从本地数据库安全读取布尔配置项。"""
    try:
        from kestrel.db.engine import connect
        from kestrel.services import config as config_service

        conn = connect()
        try:
            return bool(config_service.get(conn, key, default))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return default


def _write_bool_config(key: str, value: bool) -> None:
    """向本地数据库安全写入布尔配置项。"""
    try:
        from kestrel.db.engine import connect
        from kestrel.services import config as config_service

        conn = connect()
        try:
            config_service.put(conn, key, bool(value))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("config write skipped (%s): %s", key, exc)


def _post_local_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    """绕过系统代理向本地 127.0.0.1 服务发起 POST JSON 请求。"""
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}


def _get_objc_tray_delegate_class():
    """惰性创建并缓存 PyObjC 菜单回调代理类，避免重复注册 Objective-C 类名。"""
    global _OBJC_TRAY_DELEGATE_CLS
    if _OBJC_TRAY_DELEGATE_CLS is not None:
        return _OBJC_TRAY_DELEGATE_CLS

    import AppKit

    class _KestrelTrayMenuDelegate(AppKit.NSObject):
        def menuNeedsUpdate_(self, menu):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl._refresh_macos_menu_states()

        def onOpenWindow_(self, _sender):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.show_window()

        def onOpenBrowser_(self, _sender):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.open_browser()

        def onScanNow_(self, _sender):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.trigger_scan()

        def onTogglePrivacy_(self, _sender):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.toggle_privacy()

        def onToggleCloseToTray_(self, _sender):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.toggle_close_to_tray()

        def onQuitApp_(self, _sender):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.request_quit()

    _OBJC_TRAY_DELEGATE_CLS = _KestrelTrayMenuDelegate
    return _OBJC_TRAY_DELEGATE_CLS


def _get_objc_app_proxy_class():
    """惰性创建并缓存 macOS NSApplicationDelegate 包装类（支持 Dock 重开与 ⌘Q 退出）。"""
    global _OBJC_APP_PROXY_CLS
    if _OBJC_APP_PROXY_CLS is not None:
        return _OBJC_APP_PROXY_CLS

    import AppKit
    import Foundation

    class _KestrelAppDelegateProxy(AppKit.NSObject):
        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, has_visible):
            ctrl = getattr(self, "_controller", None)
            if not has_visible and ctrl is not None:
                ctrl.show_window()
            return Foundation.YES

        def applicationShouldTerminate_(self, app):
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.mark_quitting()
                ctrl.stop()
                if ctrl.server is not None:
                    ctrl.server.should_exit = True
                if ctrl._on_quit is not None:
                    with contextlib.suppress(Exception):
                        ctrl._on_quit()
            orig = getattr(self, "_orig_delegate", None)
            if orig is not None and hasattr(orig, "applicationShouldTerminate_"):
                return orig.applicationShouldTerminate_(app)
            return Foundation.YES

        def applicationSupportsSecureRestorableState_(self, _app):
            return Foundation.YES

    _OBJC_APP_PROXY_CLS = _KestrelAppDelegateProxy
    return _OBJC_APP_PROXY_CLS


class TrayController:
    """统一管理 macOS 原生状态栏与 Windows/Linux pystray 托盘生命周期。"""

    def __init__(
        self,
        url: str,
        window: Any = None,
        server: Any = None,
        on_quit: Any = None,
        locale: str | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.window = window
        self.server = server
        self._on_quit = on_quit
        self._locale = locale
        self._quitting = False
        self.active = False

        # 保持强引用，防止 Objective-C / pystray 对象被 GC 回收
        self._ns_status_item: Any = None
        self._ns_menu: Any = None
        self._ns_delegate: Any = None
        self._ns_privacy_item: Any = None
        self._ns_close_item: Any = None
        self._ns_app_proxy: Any = None
        self._pystray_icon: Any = None

    @property
    def quitting(self) -> bool:
        """是否已进入显式退出流程。"""
        return self._quitting

    def mark_quitting(self) -> None:
        """标记应用正在显式退出，使窗口关闭钩子放行销毁。"""
        self._quitting = True

    def attach_window(self, window: Any) -> None:
        """关联 pywebview 主窗口实例。"""
        self.window = window

    def get_close_to_tray(self) -> bool:
        """读取当前「关闭窗口时最小化到托盘」开关状态。"""
        return _read_bool_config("desktop.close_to_tray", True)

    def get_privacy(self) -> bool:
        """读取当前「金额隐私打码」开关状态。"""
        return _read_bool_config("ui.privacy", False)

    def should_intercept_close(self) -> bool:
        """判断当前窗口关闭事件是否应拦截并隐藏到托盘后台。"""
        return bool(self.active and not self._quitting and self.get_close_to_tray())

    def handle_window_closing(self) -> bool:
        """绑定到 ``window.events.closing`` 的事件回调。

        返回 ``False`` 将阻止 ``pywebview`` 销毁窗口并保持后台服务与调度器继续运行；
        返回 ``True`` 则允许窗口正常关闭并触发服务退出。
        """
        if self.should_intercept_close():
            if self.window is not None:
                with contextlib.suppress(Exception):
                    self.window.hide()
            log.info("window hidden to system tray; background scheduler continues running")
            return False

        self._quitting = True
        self.stop()
        if self.server is not None:
            self.server.should_exit = True
        if self._on_quit is not None:
            with contextlib.suppress(Exception):
                self._on_quit()
        return True

    def show_window(self) -> None:
        """恢复显示并前置激活 Kestrel 原生主窗口。"""
        if self.window is None:
            self.open_browser()
            return

        def _do_show() -> None:
            with contextlib.suppress(Exception):
                self.window.show()
            with contextlib.suppress(Exception):
                self.window.restore()
            if sys.platform == "darwin":
                with contextlib.suppress(Exception):
                    import AppKit

                    AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
            try:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(_do_show)
                return
            except Exception:  # noqa: BLE001
                pass
        _do_show()

    def open_browser(self) -> None:
        """在系统默认浏览器中打开本地 Web 工作台。"""
        with contextlib.suppress(Exception):
            webbrowser.open(self.url)

    def trigger_scan(self) -> None:
        """在后台守护线程触发一次全市场扫描，避免阻塞 UI 主线程。"""

        def _worker() -> None:
            try:
                _post_local_json(f"{self.url}/api/advice/scan", {}, timeout=60.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("tray scan request failed: %s", exc)

        threading.Thread(target=_worker, daemon=True, name="kestrel-tray-scan").start()

    def toggle_privacy(self) -> None:
        """在后台守护线程切换金额隐私打码状态，并同步通知已打开的窗口。"""

        def _worker() -> None:
            enabled: bool | None = None
            try:
                res = _post_local_json(f"{self.url}/api/ui/privacy", {}, timeout=5.0)
                if "enabled" in res:
                    enabled = bool(res["enabled"])
            except Exception:  # noqa: BLE001
                enabled = not self.get_privacy()
                _write_bool_config("ui.privacy", enabled)

            if enabled is not None and self.window is not None:
                state_str = "on" if enabled else "off"
                pressed_str = "true" if enabled else "false"
                js = (
                    f"document.documentElement.dataset.privacy='{state_str}';"
                    "var b=document.getElementById('toggle-privacy');"
                    f"if(b)b.setAttribute('aria-pressed','{pressed_str}');"
                )
                with contextlib.suppress(Exception):
                    self.window.evaluate_js(js)

        threading.Thread(target=_worker, daemon=True, name="kestrel-tray-privacy").start()

    def toggle_close_to_tray(self) -> bool:
        """切换「关闭窗口时驻留后台」配置并立即持久化。"""
        new_val = not self.get_close_to_tray()
        _write_bool_config("desktop.close_to_tray", new_val)

        def _sync_api() -> None:
            with contextlib.suppress(Exception):
                _post_local_json(
                    f"{self.url}/api/desktop/settings",
                    {"close_to_tray": new_val},
                    timeout=5.0,
                )

        threading.Thread(target=_sync_api, daemon=True, name="kestrel-tray-setting").start()
        self._refresh_macos_menu_states()
        return new_val

    def request_quit(self) -> None:
        """显式退出 Kestrel：标记退出、移除托盘、停止 uvicorn 服务并销毁窗口。"""
        self._quitting = True
        self.stop()
        if self.server is not None:
            self.server.should_exit = True
        if self._on_quit is not None:
            with contextlib.suppress(Exception):
                self._on_quit()

        if self.window is not None:
            def _destroy() -> None:
                with contextlib.suppress(Exception):
                    self.window.destroy()

            if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
                try:
                    from PyObjCTools import AppHelper

                    AppHelper.callAfter(_destroy)
                    return
                except Exception:  # noqa: BLE001
                    pass
            _destroy()
        elif sys.platform == "darwin":
            with contextlib.suppress(Exception):
                from PyObjCTools import AppHelper

                AppHelper.callAfter(AppHelper.stopEventLoop)

    # ------------------------------------------------------------------ macOS 原生后端

    def _refresh_macos_menu_states(self) -> None:
        """在展开 macOS 菜单前同步勾选状态。"""
        if self._ns_close_item is None and self._ns_privacy_item is None:
            return
        with contextlib.suppress(Exception):
            import AppKit

            on_val = getattr(AppKit, "NSControlStateValueOn", 1)
            off_val = getattr(AppKit, "NSControlStateValueOff", 0)
            if self._ns_close_item is not None:
                self._ns_close_item.setState_(on_val if self.get_close_to_tray() else off_val)
            if self._ns_privacy_item is not None:
                self._ns_privacy_item.setState_(on_val if self.get_privacy() else off_val)

    def _build_macos_tray_on_main_thread(self) -> None:
        """必须在 Cocoa 主线程调用：创建 NSStatusItem、加载 18x18 图标并挂载 NSMenu。"""
        try:
            import AppKit

            labels = menu_labels(self._locale)
            status_bar = AppKit.NSStatusBar.systemStatusBar()
            status_item = status_bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            button = status_item.button()

            icon_loaded = False
            icon_path = find_brand_icon_path()
            if icon_path is not None and button is not None:
                ns_img = AppKit.NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if ns_img is not None:
                    ns_img.setSize_((18.0, 18.0))
                    button.setImage_(ns_img)
                    button.setToolTip_("Kestrel")
                    icon_loaded = True

            if not icon_loaded and button is not None:
                button.setTitle_("隼")
                button.setToolTip_("Kestrel")

            delegate_cls = _get_objc_tray_delegate_class()
            delegate = delegate_cls.alloc().init()
            delegate._controller = self

            menu = AppKit.NSMenu.alloc().initWithTitle_("Kestrel")
            menu.setAutoenablesItems_(False)
            menu.setDelegate_(delegate)

            def _make_item(title: str, selector: str | None, enabled: bool = True):
                item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, selector, ""
                )
                if selector:
                    item.setTarget_(delegate)
                item.setEnabled_(enabled)
                return item

            menu.addItem_(_make_item(labels["open_window"], "onOpenWindow:"))
            menu.addItem_(_make_item(labels["open_browser"], "onOpenBrowser:"))
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(_make_item(labels["status_running"], None, enabled=False))
            menu.addItem_(_make_item(labels["scan_now"], "onScanNow:"))

            privacy_item = _make_item(labels["toggle_privacy"], "onTogglePrivacy:")
            close_item = _make_item(labels["close_to_tray"], "onToggleCloseToTray:")
            menu.addItem_(privacy_item)
            menu.addItem_(close_item)

            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            menu.addItem_(_make_item(labels["quit"], "onQuitApp:"))

            status_item.setMenu_(menu)

            self._ns_delegate = delegate
            self._ns_menu = menu
            self._ns_privacy_item = privacy_item
            self._ns_close_item = close_item
            self._ns_status_item = status_item
            self._refresh_macos_menu_states()
            self._install_macos_app_delegate_proxy()
            log.info("macOS native status bar item initialised")
        except Exception as exc:  # noqa: BLE001
            log.warning("macOS tray setup failed: %s", exc)
            self.active = False

    def _install_macos_app_delegate_proxy(self) -> None:
        """在 Cocoa 主线程包装 NSApp.delegate，使点击 Dock 图标重开隐藏窗口且 ⌘Q 直接退出。"""
        with contextlib.suppress(Exception):
            import AppKit

            ns_app = AppKit.NSApplication.sharedApplication()
            current = ns_app.delegate()
            proxy_cls = _get_objc_app_proxy_class()
            if current is not None and isinstance(current, proxy_cls):
                return
            proxy = proxy_cls.alloc().init()
            proxy._orig_delegate = current
            proxy._controller = self
            ns_app.setDelegate_(proxy)
            self._ns_app_proxy = proxy

    # ------------------------------------------------------------------ Windows / Linux pystray 后端

    def _start_pystray(self) -> bool:
        """在后台守护线程启动 pystray 系统托盘图标。"""
        import pystray

        labels = menu_labels(self._locale)
        menu = pystray.Menu(
            pystray.MenuItem(labels["open_window"], lambda _i, _m: self.show_window(), default=True),
            pystray.MenuItem(labels["open_browser"], lambda _i, _m: self.open_browser()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(labels["status_running"], None, enabled=False),
            pystray.MenuItem(labels["scan_now"], lambda _i, _m: self.trigger_scan()),
            pystray.MenuItem(
                labels["toggle_privacy"],
                lambda _i, _m: self.toggle_privacy(),
                checked=lambda _m: self.get_privacy(),
            ),
            pystray.MenuItem(
                labels["close_to_tray"],
                lambda _i, _m: self.toggle_close_to_tray(),
                checked=lambda _m: self.get_close_to_tray(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(labels["quit"], lambda _i, _m: self.request_quit()),
        )
        icon = pystray.Icon("kestrel", load_pil_icon(), "Kestrel", menu)
        self._pystray_icon = icon
        threading.Thread(target=icon.run, daemon=True, name="kestrel-tray").start()
        self.active = True
        return True

    # ------------------------------------------------------------------ 生命周期入口

    def start(self) -> bool:
        """启动当前平台的系统托盘。异常安全，失败时返回 False。"""
        if not tray_supported():
            self.active = False
            return False
        try:
            if sys.platform == "darwin":
                from PyObjCTools import AppHelper

                self.active = True
                AppHelper.callAfter(self._build_macos_tray_on_main_thread)
                return True
            return self._start_pystray()
        except Exception as exc:  # noqa: BLE001
            log.warning("tray unavailable: %s", exc)
            self.active = False
            return False

    def notify(self, message: str, title: str = "Kestrel") -> None:
        """发送轻量级托盘提示（pystray 支持时生效）。"""
        if self._pystray_icon is not None:
            with contextlib.suppress(Exception):
                self._pystray_icon.notify(message, title)

    def stop(self) -> None:
        """移除系统托盘图标并释放底层引用。"""
        self.active = False
        if self._pystray_icon is not None:
            icon = self._pystray_icon
            self._pystray_icon = None
            with contextlib.suppress(Exception):
                icon.stop()

        if self._ns_status_item is not None and sys.platform == "darwin":
            item = self._ns_status_item
            self._ns_status_item = None

            def _remove() -> None:
                with contextlib.suppress(Exception):
                    import AppKit

                    AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(item)

            if threading.current_thread() is threading.main_thread():
                _remove()
            else:
                with contextlib.suppress(Exception):
                    from PyObjCTools import AppHelper

                    AppHelper.callAfter(_remove)


def start_tray(
    url: str,
    on_quit: Any = None,
    *,
    window: Any = None,
    server: Any = None,
    locale: str | None = None,
) -> TrayController | None:
    """便捷工厂：启动跨平台系统托盘控制器，不可用时返回 ``None``。"""
    ctrl = TrayController(url=url, window=window, server=server, on_quit=on_quit, locale=locale)
    if ctrl.start():
        return ctrl
    return None
