"""截图脚本：浅色模式下分别截中英文两套图，供 README 使用。

用法：先起一个带演示数据的服务，然后

    KESTREL_URL=http://127.0.0.1:8899 uv run python scripts/make_screenshots.py

每页截两次（zh / en），输出 ``docs/screenshots/<page>.<locale>.png``。
浅色是 README 的默认观感：GitHub 亮色主题下深色截图会像另一款产品。

语言通过临时 profile 的 Cookie 设置，因为页面是按 `locale` cookie 渲染的；
只改 Accept-Language 不会生效——那样截出来的英文页面里会夹着中文。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PAGES = {
    "today": "/",
    "advice": "/advice",
    "journal": "/journal",
    "market": "/market",
    "welcome": "/welcome",
    "rules": "/rules",
}
SIZES = {
    "today": (1400, 1250),
    "advice": (1400, 1300),
    "journal": (1400, 1250),
    "market": (1400, 800),
    "welcome": (1400, 1280),
    "rules": (1400, 1250),
}


def capture(url: str, locale: str, target: Path, size: tuple[int, int]) -> bool:
    """用独立的 profile 截一张图，并在同一 profile 里先写入 locale cookie。"""
    profile = tempfile.mkdtemp(prefix="kestrel-shot-")
    base = [
        CHROME,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}",
        "--force-prefers-color-scheme=light",
        "--virtual-time-budget=5000",
    ]
    # 第一次访问只为了拿到域，带上 Cookie 头；headless Chrome 不会把 --header
    # 应用到后续导航，所以用 Cookie 文件方式更可靠：直接请求带 cookie 的 URL
    # 不可行，改为先打开一次设置页写入 cookie（通过 data URL + document.cookie 不可跨域）。
    # 最稳妥的做法是让服务接受 ?locale= 查询参数——见 app.py 的 _ctx。
    sep = "&" if "?" in url else "?"
    final = f"{url}{sep}locale={locale}"
    args = base + [f"--screenshot={target}", final]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return target.exists() and result.returncode == 0


def main() -> int:
    base = os.environ.get("KESTREL_URL", "http://127.0.0.1:8899").rstrip("/")
    OUT.mkdir(parents=True, exist_ok=True)
    failures = []
    for name, path in PAGES.items():
        for locale in ("zh", "en"):
            target = OUT / f"{name}.{locale}.png"
            ok = capture(f"{base}{path}", locale, target, SIZES.get(name, (1400, 1200)))
            print(f"{'ok  ' if ok else 'FAIL'} {target.relative_to(ROOT)}")
            if not ok:
                failures.append(str(target))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
