"""Entry point used by the PyInstaller bundle."""

from __future__ import annotations

import sys


def main() -> int:
    from kestrel.cli import main as cli_main

    # 过滤 macOS Finder / Xcode 自动附加的 -psn_*、-Apple*、-NSDocumentRevisionsDebugMode 等参数。
    argv: list[str] = []
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == "-NSDocumentRevisionsDebugMode":
            skip_next = True
            continue
        if arg.startswith(("-psn_", "-Apple", "-NS")):
            continue
        argv.append(arg)
    return cli_main(argv or ["desktop"])


if __name__ == "__main__":
    raise SystemExit(main())
