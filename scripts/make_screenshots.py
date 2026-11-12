"""截图脚本：浅色模式下分别截中英文两套图，供 README 使用。

用法：先起一个带演示数据的服务，然后

    KESTREL_URL=http://127.0.0.1:8899 uv run python scripts/make_screenshots.py

每个页面截两次（zh-CN / en），保存为 ``docs/screenshots/<page>.<locale>.png``。
浅色是 README 的默认观感：GitHub 的亮色主题下，深色截图会显得像另一款产品。
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
SIZES = {"today": (1400, 1250), "advice": (1400, 1300), "journal": (1400, 1250),
         "market": (1400, 800), "welcome": (1400, 1250), "rules": (1400, 1250)}


def capture(url: str, locale: str, target: Path, size: tuple[int, int]) -> bool:
    profile = tempfile.mkdtemp(prefix="kestrel-shot-")
    args = [
        CHROME,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}",
        "--force-prefers-color-scheme=light",
        f"--screenshot={target}",
        "--virtual-time-budget=4000",
        # Cookie 让页面直接用对应语言渲染，避免截图里混着两种语言。
        f"--header=Accept-Language:{'en-US,en' if locale == 'en' else 'zh-CN,zh'}",
        url,
    ]
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
            print(f"{'ok ' if ok else 'FAIL'} {target.relative_to(ROOT)}")
            if not ok:
                failures.append(str(target))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
