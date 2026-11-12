#!/usr/bin/env python3
"""生成 macOS 安装包（dmg）的背景图。

布局按 Finder 窗口的**逻辑点**排（660×400），输出 2x（1320×800）：

    0 ────────────────────────────
      │  品牌标记 + Kestrel        │  y ≈ 26–70
      │  一句说明                   │  y ≈ 78
      │                             │
      │  [app]      [Applications] │  y = 200（图标 128，占 136–264）
      │                             │
      │  拖到右侧的提示             │  y = 300
      │  ┌─ 首次打开的说明 ───────┐ │  y = 330–386
    400 ────────────────────────────

所有文字都落在图标的上下空白带里，不会压到图标——这是上一版的实际问题。
图标坐标与 ``packaging/macos/dmg_settings.py`` 里的 icon_locations 必须一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "dmg-background.png"
OUT_2X = ROOT / "packaging" / "dmg-background@2x.png"

# Finder 实际可用的内容区（扣除标题栏后）约为 660×356；按这个尺寸出图，
# 底部那行安装说明才不会被窗口裁掉。
LOGICAL_W, LOGICAL_H = 660, 356
SCALE = 2
W, H = LOGICAL_W * SCALE, LOGICAL_H * SCALE

NAVY = (22, 48, 92)
BLUE = (30, 99, 216)
TEAL = (14, 158, 142)
INK = (15, 20, 32)
MUTED = (126, 136, 153)
LINE = (228, 233, 241)


def _font(size: int, bold: bool = False, cjk: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
        ]
        if cjk
        else [
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    )
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size * SCALE, index=1 if bold else 0)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default(size * SCALE)


def _pt(x: float, y: float) -> tuple[float, float]:
    return x * SCALE, y * SCALE


def _feather(canvas: Image.Image, cx: float, cy: float, scale: float, alpha: int) -> None:
    """与 scripts/brand.py 同一几何的羽毛水印。"""
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for dy, colour in ((-50, NAVY), (0, BLUE), (50, TEAL)):
        draw.polygon(
            [
                _pt(cx + 80 * scale, cy),
                _pt(cx - 80 * scale, cy + (dy - 18) * scale),
                _pt(cx - 80 * scale, cy + (dy + 18) * scale),
            ],
            fill=colour + (alpha,),
        )
    canvas.alpha_composite(layer)


def main() -> int:
    img = Image.new("RGBA", (W, H), (248, 249, 251, 255))
    draw = ImageDraw.Draw(img)

    # 极淡的纵向渐变，避免整块死白
    for row in range(H):
        t = row / H
        value = int(250 - 7 * t)
        draw.line([(0, row), (W, row)], fill=(value, value + 1, value + 3, 255))

    # 左右两侧的水印：只做气氛，避开中间两个图标
    _feather(img, 58, 170, 1.0, 9)
    _feather(img, LOGICAL_W - 58, 170, 1.0, 9)

    # ---- 顶部品牌区（y 26–78）----
    # 用 macos 应用图标本身，而不是另画的羽毛：安装窗口里看到的标必须和
    # 用户拖进 Applications 后 Dock 里出现的那个一模一样。
    mark = Image.open(ROOT / "packaging" / "icon.png").convert("RGBA")
    # 图标自带 9.8% 的透明边距，缩到 64 时实际图形只剩 50 左右，看着太小。
    mark = mark.resize((104, 104), Image.LANCZOS)
    img.alpha_composite(mark, (int(LOGICAL_W / 2 * SCALE - 52), 18 * SCALE))

    draw.text(
        _pt(LOGICAL_W / 2, 108),
        "Kestrel",
        font=_font(22, bold=True),
        fill=NAVY,
        anchor="mm",
    )
    draw.text(
        _pt(LOGICAL_W / 2, 132),
        "先盯住，再出手  ·  Watch first. Then act.",
        font=_font(10, cjk=True),
        fill=MUTED,
        anchor="mm",
    )

    # ---- 拖拽示意：画在两个图标的**空隙**里（x 250–410，y 190），
    # 图标中心 y=186、128 宽，占 122–250；箭头放在同一水平线上。----
    arrow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ad = ImageDraw.Draw(arrow)
    # 箭头横跨两个图标之间（图标中心 170 / 490，各占 128 宽），
    # 所以从 244 画到 412，正好在空隙里，末端指向 Applications。
    ad.line(_pt(244, 186) + _pt(400, 186), fill=BLUE + (95,), width=2 * SCALE)
    ad.polygon(
        [_pt(414, 186), _pt(394, 177), _pt(394, 195)],
        fill=BLUE + (120,),
    )
    img.alpha_composite(arrow)

    # ---- 图标下方的动作说明 ----
    draw.text(
        _pt(LOGICAL_W / 2, 262),
        "把 Kestrel 拖进 Applications 文件夹",
        font=_font(10, cjk=True),
        fill=MUTED,
        anchor="mm",
    )

    # ---- 底部提示框（y 304–352）----
    box_w, box_h = 560, 46
    bx, by = (LOGICAL_W - box_w) / 2, 274
    draw.rounded_rectangle(
        _pt(bx, by) + _pt(bx + box_w, by + box_h),
        radius=10 * SCALE,
        fill=(255, 255, 255, 242),
        outline=LINE,
        width=2 * SCALE // 2,
    )
    draw.rounded_rectangle(
        _pt(bx + 1, by + 1) + _pt(bx + 3.5, by + box_h - 1),
        radius=2 * SCALE,
        fill=BLUE,
    )
    draw.text(
        _pt(bx + 16, by + 14),
        "首次打开若提示「已损坏」或「无法验证开发者」，在终端执行：",
        font=_font(8.5, cjk=True),
        fill=INK,
        anchor="lm",
    )
    draw.text(
        _pt(bx + 16, by + 30),
        "xattr -dr com.apple.quarantine /Applications/Kestrel.app",
        font=_font(8.5),
        fill=BLUE,
        anchor="lm",
    )

    # 轻微高光
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(
        _pt(-W / 6, -H / 2) + _pt(W * 1.2, H * 0.55),
        fill=(30, 99, 216, 9),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(60))
    img.alpha_composite(glow)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUT_2X, "PNG", optimize=True)
    img.convert("RGB").resize((W // 2, H // 2), Image.LANCZOS).save(OUT, "PNG", optimize=True)
    print(
        f"wrote {OUT.name} + {OUT_2X.name} "
        f"({OUT.stat().st_size // 1024} KB / {OUT_2X.stat().st_size // 1024} KB)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
