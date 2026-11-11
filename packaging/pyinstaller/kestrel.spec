# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Kestrel desktop app.

Templates, static assets, locales, SQL migrations and brand images are data
files, not code. If a new asset directory is added it must be listed in DATAS
too, otherwise the frozen app starts with a blank page.

Build with::

    uv run pyinstaller packaging/pyinstaller/kestrel.spec --noconfirm

or ``make package``, which also generates the icons and signs the bundle.
"""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent.parent
SRC = ROOT / "src" / "kestrel"

# Single source of truth: the bundle must never claim a version the wheel does not.
VERSION = re.search(
    r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M
).group(1)

datas = [
    (str(SRC / "web" / "templates"), "kestrel/web/templates"),
    (str(SRC / "web" / "static"), "kestrel/web/static"),
    (str(SRC / "i18n" / "locales"), "kestrel/i18n/locales"),
    (str(SRC / "db" / "migrations"), "kestrel/db/migrations"),
    (str(ROOT / "assets" / "brand"), "assets/brand"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "README.md"), "."),
]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "webview.platforms.cocoa",
    "webview.platforms.edgechromium",
    "pystray._darwin",
    "pystray._win32",
] + collect_submodules("kestrel")

# None of this needs the scientific stack; excluding it saves ~150 MB.
excludes = [
    "tkinter",
    "matplotlib",
    "pandas",
    "numpy",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "pytest",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kestrel",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    # Only Windows/Linux read this; macOS takes its icon from BUNDLE below.
    icon=str(ROOT / "assets" / "brand" / "favicon" / "favicon.ico")
    if sys.platform == "win32"
    else None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Kestrel")

if sys.platform == "darwin":
    BUNDLE(
        coll,
        name="Kestrel.app",
        icon=str(ROOT / "packaging" / "icon.icns"),
        bundle_identifier="dev.kestrel.app",
        version=VERSION,
        info_plist={
            "CFBundleName": "Kestrel",
            "CFBundleDisplayName": "Kestrel",
            "CFBundleExecutable": "Kestrel",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.finance",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "GPL-3.0 © Kestrel contributors",
            # The window talks to the app's own server over http://127.0.0.1.
            "NSAppTransportSecurity": {
                "NSAllowsLocalNetworking": True,
                "NSExceptionDomains": {
                    "127.0.0.1": {"NSExceptionAllowsInsecureHTTPLoads": True},
                    "localhost": {"NSExceptionAllowsInsecureHTTPLoads": True},
                },
            },
        },
    )
