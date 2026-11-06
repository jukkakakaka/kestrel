"""Desktop shell: a native window plus a tray icon around the local server.

The server keeps running when the window is closed, so scheduled scans are not
interrupted. Requires the ``desktop`` extra (pywebview, pystray, pillow).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

BRAND = Path(__file__).parents[3] / "assets" / "brand"


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
    """Open the native window and block until it closes."""
    import webview

    def quit_app():
        if server is not None:
            server.should_exit = True

    _start_tray(url, quit_app)

    webview.create_window(
        "Kestrel",
        url,
        width=1180,
        height=820,
        min_size=(900, 620),
        background_color="#0b0f16" if _prefers_dark() else "#f6f7f9",
    )
    webview.start()

    # Window closed: keep the server alive only if a tray icon is running.
    log.info("window closed")


def _prefers_dark() -> bool:
    try:
        import darkdetect

        return bool(darkdetect.isDark())
    except Exception:
        return False
