"""Desktop shell: a native window plus a tray icon around the local server.

The server keeps running when the window is closed, so scheduled scans are not
interrupted. Requires the ``desktop`` extra (pywebview, pystray, pillow).
"""

from __future__ import annotations

import contextlib
import logging
import threading

from kestrel.settings import bundle_dir

log = logging.getLogger(__name__)

BRAND = bundle_dir() / "assets" / "brand"


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
    try:
        import pystray
    except ImportError:
        log.info("pystray not installed, running without tray icon")
        return None

    def open_panel(_item=None):
        import webbrowser

        webbrowser.open(url)

    menu = pystray.Menu(
        pystray.MenuItem("Open Kestrel", open_panel, default=True),
        pystray.MenuItem("Quit", lambda: (on_quit(), icon.stop())),
    )
    icon = pystray.Icon("kestrel", _icon_image(), "Kestrel", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def run_desktop(url: str, server=None) -> None:
    """Open the native window.

    Closing the window does not kill the process when a tray icon is running:
    the scheduler keeps scanning in the background and the tray menu can bring
    the UI back. Without a tray, closing the window quits.

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
        # No window to close, so keep the server (and the scheduler) alive until
        # the process is killed from the tray or the task manager.
        try:
            while True:
                stop.wait(3600)
                if stop.is_set():
                    return
        except KeyboardInterrupt:
            return

    log.info("window closed")

    if icon is None:
        return

    with contextlib.suppress(Exception):
        icon.notify("Kestrel 仍在后台运行，点菜单栏图标可以重新打开。", "Kestrel")
    stop.wait()


def _prefers_dark() -> bool:
    try:
        import darkdetect

        return bool(darkdetect.isDark())
    except Exception:
        return False
