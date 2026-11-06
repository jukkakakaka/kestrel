# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Kestrel desktop app.

Templates, static assets, locales and SQL migrations are data files, not code —
if a new asset directory is added it must be listed in DATAS as well, otherwise the
frozen app starts with a blank page.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent.parent
SRC = ROOT / "src" / "kestrel"

datas = [
    (str(SRC / "web" / "templates"), "kestrel/web/templates"),
    (str(SRC / "web" / "static"), "kestrel/web/static"),
    (str(SRC / "i18n" / "locales"), "kestrel/i18n/locales"),
    (str(SRC / "db" / "migrations"), "kestrel/db/migrations"),
    (str(ROOT / "assets" / "brand"), "assets/brand"),
]

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "webview.platforms.cocoa",
    "webview.platforms.edgechromium",
    "pystray._darwin",
    "pystray._win32",
] + collect_submodules("kestrel")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "PyQt5", "PySide6"],
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
    icon=str(ROOT / "packaging" / "icon"),
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Kestrel")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Kestrel.app",
        icon=str(ROOT / "packaging" / "icon.icns"),
        bundle_identifier="dev.kestrel.app",
        info_plist={
            "CFBundleName": "Kestrel",
            "CFBundleDisplayName": "Kestrel 隼",
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
        },
    )
