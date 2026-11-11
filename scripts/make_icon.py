#!/usr/bin/env python3
"""生成应用图标（packaging/icon.png）。

macOS 26 起，系统会给「不是圆形方块」的旧图标自动套一层深色底板，效果通常很廉价。
所以这里自己画一个符合 Apple 图标网格的圆角方形：824×824 的内容区、185 的圆角、
藏青到蓝的对角渐变，中间是高对比的三片羽毛。透明四角 + 正确网格，
新系统无论套不套壳都不会显得突兀。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packaging" / "icon.png"

SIZE = 1024
MARGIN = 100
CORNER = 185

NAVY = (20, 40, 78)
BLUE = (34, 105, 224)
TEAL = (16, 168, 150)


def _gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(1, size - 1)
        # 对角线方向：右上更亮
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * (t ** 0.9)) for i in range(3))
        draw.line([(0, y), (size, y)], fill=colour)
    return img


def main() -> int:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 内容区：Apple 的 macOS 图标网格
    inner = SIZE - MARGIN * 2
    base = _gradient(inner, (26, 56, 108), (18, 96, 208)).convert("RGBA")

    # 左上角的高光，让平面渐变有一点体积
    highlight = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.ellipse([-inner * 0.35, -inner * 0.55, inner * 0.85, inner * 0.45],
               fill=(120, 170, 255, 60))
    highlight = highlight.filter(ImageFilter.GaussianBlur(120))
    base.alpha_composite(highlight)

    # 圆角蒙版
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner - 1, inner - 1],
                                           radius=CORNER, fill=255)
    shaped = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    shaped.paste(base, (0, 0), mask)

    # 内描边：1px 半透明白，模拟玻璃边缘
    edge = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [3, 3, inner - 4, inner - 4], radius=CORNER - 3,
        outline=(255, 255, 255, 36), width=2,
    )
    shaped.alpha_composite(edge)

    # 三片羽毛：等比放大到图标中心，白色与青色提高对比
    feather = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    fd = ImageDraw.Draw(feather)
    # 三角形的视觉重心在靠底边三分之一处，所以整体右移一点才「看起来居中」。
    cx, cy = inner * 0.58, inner * 0.50
    # 原始 256 视图里羽毛宽 160px；这里让它在图标内占约 54% 宽，留出呼吸空间。
    scale = inner * 0.54 / 160
    for dy, colour in ((-68, (255, 255, 255)), (0, (170, 210, 255)), (68, TEAL)):
        fd.polygon(
            [
                (cx + 80 * scale, cy),
                (cx - 80 * scale, cy + (dy - 36) * scale),
                (cx - 80 * scale, cy + (dy + 36) * scale),
            ],
            fill=colour + (255,),
        )
    # 羽毛之间的细缝：让三片读得开
    glow = feather.filter(ImageFilter.GaussianBlur(1.2))
    shaped.alpha_composite(glow)

    canvas.alpha_composite(shaped, (MARGIN, MARGIN))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")

    # 应用内 favicon 也顺手更新一份，保持品牌一致
    for size in (180, 256, 512):
        target = ROOT / "assets" / "brand" / "favicon" / (
            f"favicon-{size}.png" if size != 180 else "favicon-180.png"
        )
        canvas.resize((size, size), Image.LANCZOS).save(target, "PNG", optimize=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
