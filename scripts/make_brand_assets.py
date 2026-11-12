#!/usr/bin/env python3
"""从唯一来源（scripts/brand.py）重新生成全部品牌资产。

    uv run python scripts/make_brand_assets.py

生成物：
  assets/brand/logo-mark.svg / logo.svg      网页与 README 用的矢量（自动深浅色）
  assets/brand/png/logo-mark-{1024,512}.png  浅色方块标记
  assets/brand/png/logo-mark-dark-{1024,512}.png
  assets/brand/favicon/*                     站点图标（浅色版）
  src/kestrel/web/static/brand/*             打包进应用的那一份
  src/kestrel/web/static/favicon/*
  packaging/icon.png / icon-dark.png         回退用的静态图标
  packaging/icon.icns                        回退用（Asset Catalog 失败时）
  packaging/Assets.xcassets/AppIcon.appiconset
                                             macOS 13+ 的深浅色图标源，build.sh
                                             用 actool 编成 Assets.car
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brand  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets" / "brand"
WEB_BRAND = ROOT / "src" / "kestrel" / "web" / "static" / "brand"
WEB_FAVICON = ROOT / "src" / "kestrel" / "web" / "static" / "favicon"
PACKAGING = ROOT / "packaging"

FAVICON_SIZES = (16, 32, 48, 64, 128, 180, 256, 512)
# macOS 的 AppIcon 槽位：(size, scale)
ICON_SLOTS = [(s, scale) for s in (16, 32, 128, 256, 512) for scale in (1, 2)]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _iconset_from(icon: Image.Image, target: Path) -> None:
    """生成 .icns。Asset Catalog 不可用时的回退路径。"""
    tmp = target.parent / ".Kestrel.iconset"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(tmp / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(
            tmp / f"icon_{size}x{size}@2x.png"
        )
    subprocess.run(["iconutil", "-c", "icns", str(tmp), "-o", str(target)], check=True)
    shutil.rmtree(tmp)


def _asset_catalog(light: Image.Image, dark: Image.Image) -> Path:
    """macOS 13+ 的 AppIcon asset：浅色与深色各一套。

    `.icns` 只有一种外观；要让 Dock 跟随系统深浅色，必须走 Asset Catalog
    （编译成 Assets.car，再由 Info.plist 的 CFBundleIconName 引用）。
    """
    catalog = PACKAGING / "Assets.xcassets"
    appicon = catalog / "AppIcon.appiconset"
    if catalog.exists():
        shutil.rmtree(catalog)
    appicon.mkdir(parents=True)
    _write(
        catalog / "Contents.json",
        json.dumps({"info": {"author": "xcode", "version": 1}}, indent=2),
    )

    images = []
    for size, scale in ICON_SLOTS:
        px = size * scale
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
        light.resize((px, px), Image.LANCZOS).save(appicon / name)
        images.append({"filename": name, "idiom": "mac", "scale": f"{scale}x",
                       "size": f"{size}x{size}"})
    for size, scale in ICON_SLOTS:
        px = size * scale
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}-dark.png"
        dark.resize((px, px), Image.LANCZOS).save(appicon / name)
        images.append({
            "filename": name,
            "idiom": "mac",
            "scale": f"{scale}x",
            "size": f"{size}x{size}",
            "appearances": [{"appearance": "luminosity", "value": "dark"}],
        })

    _write(
        appicon / "Contents.json",
        json.dumps({"images": images, "info": {"author": "xcode", "version": 1}}, indent=2),
    )
    return catalog


def main() -> int:
    # ---- 矢量 ----
    _write(ASSETS / "logo-mark.svg", brand.svg_mark(tile=True))
    _write(ASSETS / "logo.svg", brand.svg_logo())

    # ---- 方块标记 PNG（深浅各一套）----
    (ASSETS / "png").mkdir(parents=True, exist_ok=True)
    brand.mark_image(1024).save(ASSETS / "png" / "logo-mark-1024.png", optimize=True)
    brand.mark_image(512).save(ASSETS / "png" / "logo-mark-512.png", optimize=True)
    brand.mark_image(1024, variant="dark").save(
        ASSETS / "png" / "logo-mark-dark-1024.png", optimize=True
    )
    brand.mark_image(512, variant="dark").save(
        ASSETS / "png" / "logo-mark-dark-512.png", optimize=True
    )

    # ---- 应用图标 ----
    light = brand.icon_image(1024, "light")
    dark = brand.icon_image(1024, "dark")
    light.save(PACKAGING / "icon.png", optimize=True)
    dark.save(PACKAGING / "icon-dark.png", optimize=True)
    _iconset_from(light, PACKAGING / "icon.icns")
    catalog = _asset_catalog(light, dark)

    # ---- favicon ----
    favicon_dir = ASSETS / "favicon"
    favicon_dir.mkdir(parents=True, exist_ok=True)
    for size in FAVICON_SIZES:
        light.resize((size, size), Image.LANCZOS).save(
            favicon_dir / f"favicon-{size}.png", optimize=True
        )
    light.save(
        favicon_dir / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )

    # ---- 打包进应用的一份 ----
    WEB_BRAND.mkdir(parents=True, exist_ok=True)
    WEB_FAVICON.mkdir(parents=True, exist_ok=True)
    for name in ("logo-mark.svg", "logo.svg"):
        shutil.copyfile(ASSETS / name, WEB_BRAND / name)
    for path in favicon_dir.iterdir():
        shutil.copyfile(path, WEB_FAVICON / path.name)

    print("brand assets regenerated:")
    for path in (
        ASSETS / "logo-mark.svg",
        ASSETS / "png" / "logo-mark-1024.png",
        ASSETS / "png" / "logo-mark-dark-1024.png",
        PACKAGING / "icon.png",
        PACKAGING / "icon-dark.png",
        PACKAGING / "icon.icns",
        catalog,
    ):
        kind = "" if path.is_dir() else f"  ({path.stat().st_size // 1024} KB)"
        print(f"  {path.relative_to(ROOT)}{kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
