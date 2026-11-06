"""Kestrel command line interface."""

from __future__ import annotations

import argparse
import contextlib
import logging
import socket
import sys
import threading
import time

from kestrel import __version__
from kestrel.settings import settings


def _free_port(start: int, attempts: int = 20) -> int:
    for offset in range(attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port found")


def _serve(host: str, port: int):
    import uvicorn

    from kestrel.app import app

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    return uvicorn.Server(config)


def cmd_init(_args: argparse.Namespace) -> int:
    from kestrel.db.migrate import init_db

    settings.ensure_dirs()
    applied = init_db()
    print(f"Kestrel {__version__}")
    print(f"  data dir : {settings.home}")
    print(f"  database : {settings.db_path}")
    print(f"  migrations applied: {applied}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from kestrel.db.migrate import init_db

    settings.ensure_dirs()
    init_db()
    port = args.port or _free_port(settings.port)
    print(f"Kestrel serving on http://{args.host}:{port}")
    server = _serve(args.host, port)
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    from kestrel.db.migrate import init_db

    settings.ensure_dirs()
    init_db()

    port = _free_port(args.port or settings.port)
    server = _serve("127.0.0.1", port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(50):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)

    url = f"http://127.0.0.1:{port}"

    try:
        from kestrel.desktop.app import run_desktop
    except ImportError:
        print("desktop extras missing. Install with: uv sync --extra desktop")
        return 1

    try:
        run_desktop(url, server)
    finally:
        server.should_exit = True
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kestrel", description="Kestrel trading companion")
    parser.add_argument("--version", action="version", version=f"kestrel {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="initialise the local database")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="run the local web server")
    p_serve.add_argument("--host", default=settings.host)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_desk = sub.add_parser("desktop", help="run the desktop client (window + tray)")
    p_desk.add_argument("--port", type=int, default=None)
    p_desk.set_defaults(func=cmd_desktop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
