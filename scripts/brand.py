"""品牌资产的唯一来源。

同一个标记出现在五个地方：应用图标、Dock/iTunesArtwork、网页顶栏、favicon、
README 与 dmg 安装窗口。以前每个地方各画一遍，改了一处其余四处就旧了；现在
所有文件都由这个模块生成，几何与配色只有这一份定义。

标记本身是三片羽毛：深藏青（趋势）、亮蓝（行动）、青绿（纪律），从左边缘
汇聚到右侧一点。它总是放在一个圆角方块里——macOS 需要方块图标，网页顶栏
与 README 用同一个方块才叫「统一」，否则用户会以为看到了两个产品。

深色模式：图标、SVG 都有对应的一套。
* SVG 用 ``prefers-color-scheme`` 自动切换，浏览器里不需要额外处理；
* macOS 的 `.icns` 只有一种外观，要跟随系统必须走 Asset Catalog
  （``Assets.car`` + ``CFBundleIconName``），由 build.sh 用 actool 生成。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent

# 品牌三色
NAVY = "#16305C"
BLUE = "#1E63D8"
TEAL = "#0E9E8E"

# 深色底上羽毛要更亮，否则在 #111 上会糊成一团
NAVY_DARK = "#FFFFFF"
BLUE_DARK = "#9CC2FF"
TEAL_DARK = "#4FD9C8"

HALF_WIDTH = 80.0          # 256 视图里羽毛宽 160（x = 48 → 208）
HALF_HEIGHT = 18.0
FEATHER_OFFSETS = (-50.0, 0.0, 50.0)

LIGHT_TILE = ((255, 255, 255), (226, 235, 248))
DARK_TILE = ((38, 46, 60), (13, 17, 23))
LIGHT_FEATHERS = ((22, 48, 92), (30, 99, 216), (14, 158, 142))
DARK_FEATHERS = ((255, 255, 255), (156, 194, 255), (79, 217, 200))


def feathers(variant: str = "light") -> list[tuple[list[tuple[float, float]], tuple[int, int, int]]]:
    """三片羽毛的多边形，坐标以 256×256 视图为单位，中心在 (128,128)。"""
    colours = DARK_FEATHERS if variant == "dark" else LIGHT_FEATHERS
    out = []
    for dy, rgb in zip(FEATHER_OFFSETS, colours, strict=True):
        out.append(
            (
                [
                    (128 + HALF_WIDTH, 128.0),
                    (128 - HALF_WIDTH, 128 + dy - HALF_HEIGHT),
                    (128 - HALF_WIDTH, 128 + dy + HALF_HEIGHT),
                ],
                rgb,
            )
        )
    return out


def _scaled(points, cx: float, cy: float, scale: float):
    return [(cx + (x - 128) * scale, cy + (y - 128) * scale) for x, y in points]


def mark_image(size: int, variant: str = "light", tile: bool = True) -> Image.Image:
    """一个方形标记。

    ``tile=True``（默认）是圆角方块 + 羽毛，与应用图标同款；
    ``tile=False`` 只有羽毛，用于需要叠在别的东西上的场景。
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if tile:
        margin = size * 0.098
        inner = size - margin * 2
        radius = inner * 0.225
        top, bottom = DARK_TILE if variant == "dark" else LIGHT_TILE

        base = Image.new("RGB", (int(inner), int(inner)))
        draw = ImageDraw.Draw(base)
        for y in range(int(inner)):
            t = y / max(1, inner - 1)
            draw.line(
                [(0, y), (inner, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
            )
        base = base.convert("RGBA")

        # 左上高光：浅色底要一点体积感，深色底要一点反光
        highlight = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(highlight).ellipse(
            [-inner * 0.4, -inner * 0.6, inner * 0.9, inner * 0.4],
            fill=(255, 255, 255, 120 if variant == "light" else 26),
        )
        highlight = highlight.filter(ImageFilter.GaussianBlur(inner * 0.12))
        base.alpha_composite(highlight)

        mask = Image.new("L", base.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, base.size[0] - 1, base.size[1] - 1], radius=radius, fill=255
        )
        tile_img = Image.new("RGBA", base.size, (0, 0, 0, 0))
        tile_img.paste(base, (0, 0), mask)

        edge = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(edge).rounded_rectangle(
            [2, 2, base.size[0] - 3, base.size[1] - 3],
            radius=max(1, radius - 2),
            outline=(255, 255, 255, 28) if variant == "dark" else (22, 48, 92, 30),
            width=max(1, int(size / 340)),
        )
        tile_img.alpha_composite(edge)

        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        draw_layer = ImageDraw.Draw(layer)
        cx, cy = inner * 0.545, inner * 0.50
        scale = inner * 0.56 / 160.0
        for points, rgb in feathers(variant):
            draw_layer.polygon(_scaled(points, cx, cy, scale), fill=rgb + (255,))
        tile_img.alpha_composite(layer)

        img.alpha_composite(tile_img, (int(margin), int(margin)))
        return img

    draw = ImageDraw.Draw(img)
    margin = size * 0.08
    usable = size - margin * 2
    for points, rgb in feathers(variant):
        draw.polygon(
            _scaled(points, size / 2, size / 2, usable / 160.0), fill=rgb + (255,)
        )
    return img


def icon_image(size: int = 1024, variant: str = "light") -> Image.Image:
    return mark_image(size, variant=variant, tile=True)


def svg_mark(tile: bool = True) -> str:
    """与 PNG 完全同几何的 SVG，按系统深浅色自动切换。"""
    light = "\n".join(
        f'      <polygon points="{_svg_points(points)}" fill="{_hex(rgb)}"/>'
        for points, rgb in feathers("light")
    )
    dark = "\n".join(
        f'      <polygon points="{_svg_points(points)}" fill="{_hex(rgb)}"/>'
        for points, rgb in feathers("dark")
    )
    if not tile:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" '
            'width="256" height="256" role="img" aria-label="Kestrel">\n'
            "  <title>Kestrel</title>\n"
            "  <style>\n"
            "    .light { display: inline; } .dark { display: none; }\n"
            "    @media (prefers-color-scheme: dark) {\n"
            "      .light { display: none; } .dark { display: inline; }\n"
            "    }\n"
            "  </style>\n"
            '  <g class="light">\n'
            f"{light}\n"
            "  </g>\n"
            '  <g class="dark">\n'
            f"{dark}\n"
            "  </g>\n"
            "</svg>\n"
        )

    margin, radius = 100, 185
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" '
        'width="1024" height="1024" role="img" aria-label="Kestrel">\n'
        "  <title>Kestrel</title>\n"
        "  <defs>\n"
        '    <linearGradient id="tileLight" x1="0" y1="0" x2="0" y2="1">\n'
        '      <stop offset="0%" stop-color="#ffffff"/>\n'
        '      <stop offset="100%" stop-color="#e2ebf8"/>\n'
        "    </linearGradient>\n"
        '    <linearGradient id="tileDark" x1="0" y1="0" x2="0" y2="1">\n'
        '      <stop offset="0%" stop-color="#262e3c"/>\n'
        '      <stop offset="100%" stop-color="#0d1117"/>\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        "  <style>\n"
        "    .light { display: inline; } .dark { display: none; }\n"
        "    @media (prefers-color-scheme: dark) {\n"
        "      .light { display: none; } .dark { display: inline; }\n"
        "    }\n"
        "  </style>\n"
        '  <g class="light">\n'
        f'    <rect x="{margin}" y="{margin}" width="{1024 - margin * 2}" '
        f'height="{1024 - margin * 2}" rx="{radius}" fill="url(#tileLight)"/>\n'
        + _svg_feathers_1024("light")
        + "\n  </g>\n"
        '  <g class="dark">\n'
        f'    <rect x="{margin}" y="{margin}" width="{1024 - margin * 2}" '
        f'height="{1024 - margin * 2}" rx="{radius}" fill="url(#tileDark)"/>\n'
        + _svg_feathers_1024("dark")
        + "\n  </g>\n"
        "</svg>\n"
    )


def _svg_feathers_1024(variant: str) -> str:
    inner = 824
    margin = 100
    cx = margin + inner * 0.545
    cy = margin + inner * 0.50
    scale = inner * 0.56 / 160.0
    lines = []
    for points, rgb in feathers(variant):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in _scaled(points, cx, cy, scale))
        lines.append(f'    <polygon points="{pts}" fill="{_hex(rgb)}"/>')
    return "\n".join(lines)


def _svg_points(points) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def svg_logo() -> str:
    """横向组合：标记 + 字标（README / 网页可按需使用）。"""
    word = "Kestrel"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1180 300" width="1180" '
        'height="300" role="img" aria-label="Kestrel 隼">\n'
        "  <title>Kestrel 隼</title>\n"
        '  <g transform="translate(20,22) scale(1.0)">\n'
        + _svg_feathers_1024("light").replace("    ", "    ")
        + "\n  </g>\n"
        '  <text x="330" y="196" font-family="Helvetica Neue,Helvetica,Arial,sans-serif" '
        'font-size="150" font-weight="800" font-style="italic" letter-spacing="-5" '
        f'fill="{NAVY}">{word}</text>\n'
        '  <text x="852" y="192" font-family="PingFang SC,Microsoft YaHei,sans-serif" '
        f'font-size="128" font-weight="700" fill="{NAVY}">隼</text>\n'
        "</svg>\n"
    )
