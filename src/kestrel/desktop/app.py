"""Desktop shell: a native window around the local server.

Requires the ``desktop`` extra (pywebview, pystray, pillow).

Platform note. On macOS AppKit requires ``NSStatusItem`` to be created on the
main thread, and the main thread already belongs to pywebview's run loop.
pystray's darwin backend builds the item on whatever thread called ``run()``,
so putting it on a worker thread traps inside BoardServices
(``-[BSServiceMainRunLoopQueue assertBarrierOnQueue]``) and kills the process a
second after launch. There is only one main thread and pywebview needs it, so
the menu-bar icon is simply not created on macOS: the window is the app, closing
it quits, and ``kestrel serve`` is the way to scan headless.

Windows and Linux backends own their own message loop and are fine on a worker
thread, so the tray stays there. ``KESTREL_TRAY=0`` disables it anywhere.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading

from kestrel.settings import bundle_dir

log = logging.getLogger(__name__)

BRAND = bundle_dir() / "assets" / "brand"

_FALSY = ("0", "false", "no", "off")


def tray_supported() -> bool:
    """Whether a menu-bar / notification-area icon can be created here."""
    flag = os.environ.get("KESTREL_TRAY", "").strip().lower()
    if flag in _FALSY:
        return False
    if sys.platform == "darwin":
        # Never: creating NSStatusItem off the main thread is a hard crash, and
        # the main thread is taken by the window. See the module docstring.
        return False
    return True


def _icon_image():
    from PIL import Image

    for candidate in (
        BRAND / "favicon" / "favicon-180.png",
        BRAND / "png" / "logo-mark-512.png",
    ):
        if candidate.exists():
            return Image.open(candidate)
    return Image.new("RGBA", (64, 64), (22, 48, 92, 255))


def _start_tray(url: str, on_quit):
    """Start the tray icon, or return None. Never raises."""
    if not tray_supported():
        return None
    try:
        import pystray

        def open_panel(_item=None):
            import webbrowser

            webbrowser.open(url)

        menu = pystray.Menu(
            pystray.MenuItem("Open Kestrel", open_panel, default=True),
            pystray.MenuItem("Quit", lambda: (on_quit(), icon.stop())),
        )
        icon = pystray.Icon("kestrel", _icon_image(), "Kestrel", menu)
        threading.Thread(target=icon.run, daemon=True, name="kestrel-tray").start()
        return icon
    except Exception as exc:  # noqa: BLE001
        # A tray icon is a convenience; it must never be the reason the app dies.
        log.warning("tray unavailable: %s", exc)
        return None


def run_desktop(url: str, server=None) -> None:
    """Open the native window and block until the app should quit.

    With a tray icon the process outlives the window so the scheduler keeps
    running; without one, closing the window quits, which is the macOS norm.

    If the window cannot be created at all (no display, broken WKWebView), fall
    back to the system browser and keep serving. Silently exiting would look
    like the app is broken with nothing on screen to explain it.
    """
    import time

    stop = threading.Event()

    def quit_app():
        if server is not None:
            server.should_exit = True
        stop.set()

    icon = _start_tray(url, quit_app)

    started = time.monotonic()
    window_shown = False
    try:
        import webview

        webview.create_window(
            "Kestrel",
            url,
            width=1180,
            height=820,
            min_size=(900, 620),
            background_color="#0b0f16" if _prefers_dark() else "#f6f7f9",
        )
        webview.start()
        window_shown = time.monotonic() - started > 1.0
    except Exception as exc:  # noqa: BLE001
        log.warning("native window unavailable: %s", exc)

    if not window_shown:
        import webbrowser

        log.info("falling back to the browser at %s", url)
        with contextlib.suppress(Exception):
            webbrowser.open(url)
        # No window and possibly no tray: keep serving until the process is
        # stopped from the tray, the dock or the task manager.
        try:
            while not stop.is_set():
                stop.wait(3600)
        except KeyboardInterrupt:
            pass
        return

    log.info("window closed")

    if icon is None:
        return

    with contextlib.suppress(Exception):
        icon.notify("Kestrel is still running. Use the tray icon to reopen it.", "Kestrel")
    stop.wait()


def _prefers_dark() -> bool:
    try:
        import darkdetect

        return bool(darkdetect.isDark())
    except Exception:
        return False
