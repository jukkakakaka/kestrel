# -*- coding: utf-8 -*-
"""dmgbuild 设置：决定安装窗口长什么样。

由 ``packaging/macos/build.sh`` 调用：:

    dmgbuild -s packaging/macos/dmg_settings.py \
             -D app=dist/Kestrel.app \
             -D background=packaging/dmg-background.png \
             Kestrel dist/Kestrel-0.3.0.dmg

坐标是 Finder 的「逻辑点」，窗口 660×400；背景图按 2x 输出（1320×800），
两者必须对得上，否则图标会落在文字上。
"""

import os

app_path = defines.get("app", "dist/Kestrel.app")  # noqa: F821 — injected by dmgbuild
app_name = os.path.basename(app_path)
background_path = defines.get("background", "packaging/dmg-background.png")
icon_path = defines.get("icon", "packaging/icon.icns")

# 固定 HFS+：UDZO 压缩镜像 + HFS+ 才能在 Finder 里保留图标位置与背景图。
format = "UDZO"
filesystem = "HFS+"

files = [app_path]
symlinks = {"Applications": "/Applications"}

# 图标位置与背景图上的两个空位完全对齐（背景图坐标 ÷ 2）。
icon_locations = {
    app_name: (170, 186),
    "Applications": (490, 186),
}

background = background_path
# 窗口比背景图高 28pt：macOS 的标题栏占掉这么多，
# 用 660×428 才能让 400 高的背景图完整显示（含底部那行 xattr 说明）。
window_rect = ((240, 140), (660, 384))
default_view = "icon-view"

show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
sidebar_width = 0

icon_size = 128
text_size = 13
label_pos = "bottom"

# 挂载后 Finder 里的卷标：名字 + 品牌图标
volume_name = defines.get("volume_name", "Kestrel")
if os.path.exists(icon_path):
    icon = icon_path
