#!/usr/bin/env python3
"""从唯一来源（scripts/brand.py）重新生成全部品牌资产。

logo 会出现在：应用图标、网页顶栏、favicon、README、dmg 安装窗口。
改一次就要全部重新生成，否则用户会在四个地方看到四个不同的标。

    uv run python scripts/make_brand_assets.py

生成物：
  assets/brand/logo-mark.svg / logo.svg          网页与 README 用的矢量
  assets/brand/png/logo-mark-1024.png / -512.png  透明底标记
  assets/brand/favicon/favicon-*.png / .ico       站点图标
  src/kestrel/web/static/brand/*.svg              打包进应用的那一份
  src/kestrel/web/static/favicon/*                同上
  packaging/icon.png                              macOS / PyInstaller 图标
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brand  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets" / "brand"
WEB_BRAND = ROOT / "src" / "kestrel" / "web" / "static" / "brand"
WEB_FAVICON = ROOT / "src" / "kestrel" / "web" / "static" / "favicon"

FAVICON_SIZES = (16, 32, 48, 64, 128, 180, 256, 512)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    # ---- 矢量 ----
    _write(ASSETS / "logo-mark.svg", brand.svg_mark())
    _write(ASSETS / "logo.svg", brand.svg_logo())

    # ---- 透明底标记 PNG ----
    (ASSETS / "png").mkdir(parents=True, exist_ok=True)
    brand.mark_image(1024).save(ASSETS / "png" / "logo-mark-1024.png", optimize=True)
    brand.mark_image(512).save(ASSETS / "png" / "logo-mark-512.png", optimize=True)

    # ---- 应用图标：浅色圆角方块 + 同一个标记 ----
    icon = brand.icon_image(1024)
    icon.save(ROOT / "packaging" / "icon.png", optimize=True)

    favicon_dir = ASSETS / "favicon"
    favicon_dir.mkdir(parents=True, exist_ok=True)
    for size in FAVICON_SIZES:
        icon.resize((size, size), Image.LANCZOS).save(
            favicon_dir / f"favicon-{size}.png", optimize=True
        )
    icon.save(
        favicon_dir / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64)],
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
        ASSETS / "logo.svg",
        ASSETS / "png" / "logo-mark-1024.png",
        ASSETS / "favicon" / "favicon-512.png",
        ROOT / "packaging" / "icon.png",
    ):
        print(f"  {path.relative_to(ROOT)}  ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
