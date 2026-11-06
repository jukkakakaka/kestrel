"""Entry point used by the PyInstaller bundle."""

from __future__ import annotations

import sys


def main() -> int:
    from kestrel.cli import main as cli_main

    argv = sys.argv[1:] or ["desktop"]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
