"""Entry point used by the PyInstaller bundle."""

from __future__ import annotations

import sys


def main() -> int:
    from kestrel.cli import main as cli_main

    # Finder passes "-psn_0_123456" on some macOS versions, and an -AppleLanguages
    # style flag can show up too; argparse would reject both.
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    return cli_main(argv or ["desktop"])


if __name__ == "__main__":
    raise SystemExit(main())
