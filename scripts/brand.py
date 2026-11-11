"""品牌资产的唯一来源。

logo 出现在四个地方：应用图标、网页顶栏、README、dmg 安装窗口。
它们必须**完全一致**——同一个几何、同一组颜色，只是背景不同。
以前每处各画一遍，改了一处另外三处就旧了，所以这里只留一份定义，
其余脚本都从这里取。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent

# 品牌三色：深藏青（趋势）、亮蓝（行动）、青绿（纪律）
NAVY = "#16305C"
BLUE = "#1E63D8"
TEAL = "#0E9E8E"

PALETTE = (
    (NAVY, (22, 48, 92)),
    (BLUE, (30, 99, 216)),
    (TEAL, (14, 158, 142)),
)

# 羽毛几何，单位是 256×256 的视图框，与 logo-mark.svg 完全一致。
VIEW = 256.0
EXPECTED_CENTER = (128.0, 128.0)
HALF_WIDTH = 80.0          # x: 48 → 208
HALF_HEIGHT = 18.0         # 每片羽毛在左边缘的高度
FEATHER_OFFSETS = (-50.0, 0.0, 50.0)


def feather_polygons(cx: float, cy: float, scale: float) -> list[tuple[list[tuple[float, float]], tuple[int, int, int]]]:
    """三片羽毛，从左边缘指向右侧同一点。

    ``scale`` 以 256 视图为 1.0；几何来自原始 SVG：
    左边缘 x=48、右端点 x=208（宽 160），三片在 y=60..196 之间均分。
    """
    polygons = []
    # 与 logo-mark.svg 逐点一致：三片分别位于 y = 60–96 / 110–146 / 160–196，
    # 右端汇聚在 (208,128)。即中心偏移 −50 / 0 / +50，半高 18，宽度 160。
    for dy, (_, rgb) in zip(FEATHER_OFFSETS, PALETTE, strict=True):
        points = [
            (cx + HALF_WIDTH * scale, cy),
            (cx - HALF_WIDTH * scale, cy + (dy - HALF_HEIGHT) * scale),
            (cx - HALF_WIDTH * scale, cy + (dy + HALF_HEIGHT) * scale),
        ]
        polygons.append((points, rgb))
    return polygons


def draw_mark(draw: ImageDraw.ImageDraw, cx: float, cy: float, scale: float) -> None:
    """把标记画到已有的画布上（透明背景，品牌三色）。"""
    for points, rgb in feather_polygons(cx, cy, scale):
        draw.polygon(points, fill=rgb + (255,))


def mark_image(size: int, margin: float = 0.10) -> Image.Image:
    """透明背景的标记 PNG，尺寸为 ``size``。

    标记本身只占 160×136 的视图；按 256 视图缩放到画布后留出边距，
    这样在网页里和其它图标并排时视觉大小一致。
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    usable = size * (1 - margin * 2)
    draw_mark(draw, size / 2, size / 2, usable / 160.0)
    return img


def icon_image(size: int = 1024) -> Image.Image:
    """应用图标：浅色圆角方块 + 同一个标记。

    macOS 会给不合规的图标自动套一层深色底板，看起来像别人的 app。
    这里按 Apple 的网格自己画：824/1024 的内容区、185 的圆角、透明四角，
    标记用同一套品牌色，和网页、README、dmg 里看到的完全是同一个东西。
    """
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    margin = int(size * 0.098)
    inner = size - margin * 2
    radius = int(inner * 0.225)

    # 浅色渐变底：上白下淡蓝，贴近站点浅色主题
    base = Image.new("RGB", (inner, inner))
    bd = ImageDraw.Draw(base)
    top = (255, 255, 255)
    bottom = (226, 235, 248)
    for y in range(inner):
        t = y / max(1, inner - 1)
        bd.line(
            [(0, y), (inner, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    base = base.convert("RGBA")

    # 左上高光，让平面渐变有点体积
    highlight = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        [-inner * 0.4, -inner * 0.6, inner * 0.9, inner * 0.4],
        fill=(255, 255, 255, 130),
    )
    highlight = highlight.filter(ImageFilter.GaussianBlur(inner * 0.12))
    base.alpha_composite(highlight)

    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner - 1, inner - 1], radius=radius, fill=255)
    shaped = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    shaped.paste(base, (0, 0), mask)

    # 内描边：浅色图标在白底上需要一点边界
    edge = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [2, 2, inner - 3, inner - 3],
        radius=radius - 2,
        outline=(22, 48, 92, 28),
        width=max(2, size // 320),
    )
    shaped.alpha_composite(edge)

    # 标记：占内容区宽度约 56%，
    # 三角形的视觉重心偏左，整体右移 4% 才「看起来居中」。
    mark_layer = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    draw_mark(ImageDraw.Draw(mark_layer), inner * 0.545, inner * 0.50, inner * 0.56 / 160.0)
    shaped.alpha_composite(mark_layer)

    canvas.alpha_composite(shaped, (margin, margin))
    return canvas


def svg_mark(background: str | None = None) -> str:
    """与 PNG 同几何的 SVG 标记，可直接放进网页。"""
    paths = []
    for dy, (hex_colour, _) in zip(FEATHER_OFFSETS, PALETTE, strict=True):
        paths.append(
            f'  <path d="M208 128 L48 {128 + dy - HALF_HEIGHT:g} '
            f'L48 {128 + dy + HALF_HEIGHT:g} Z" fill="{hex_colour}"/>'
        )
    body = "\n".join(paths)
    rect = (
        f'  <rect width="256" height="256" rx="56" fill="{background}"/>\n'
        if background
        else ""
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" '
        'height="256" role="img" aria-label="Kestrel">\n'
        "  <title>Kestrel</title>\n"
        "  <desc>Three feathers fanning from one point: a wing, and a forward strike.</desc>\n"
        f"{rect}{body}\n</svg>\n"
    )


def svg_logo(background: str | None = None) -> str:
    """横向组合：标记 + 字标。"""
    rect = (
        f'  <rect width="1180" height="300" rx="48" fill="{background}"/>\n'
        if background
        else ""
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1180 300" width="1180" '
        'height="300" role="img" aria-label="Kestrel 隼">\n'
        "  <title>Kestrel 隼</title>\n"
        "  <desc>Kestrel wordmark with the three-feather wing mark.</desc>\n"
        f"{rect}"
        '  <g transform="translate(20,22)">\n'
        + "\n".join(
            f'    <path d="M208 128 L48 {128 + dy - HALF_HEIGHT:g} '
            f'L48 {128 + dy + HALF_HEIGHT:g} Z" fill="{hex_colour}"/>'
            for dy, (hex_colour, _) in zip(FEATHER_OFFSETS, PALETTE, strict=True)
        )
        + "\n  </g>\n"
        '  <text x="330" y="196" font-family="Helvetica Neue,Helvetica,Arial,sans-serif" '
        'font-size="150" font-weight="800" font-style="italic" letter-spacing="-5" '
        f'fill="{NAVY}">Kestrel</text>\n'
        '  <text x="852" y="192" font-family="PingFang SC,Microsoft YaHei,sans-serif" '
        f'font-size="128" font-weight="700" fill="{NAVY}">隼</text>\n'
        "</svg>\n"
    )
