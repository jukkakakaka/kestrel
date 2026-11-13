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


def _log_to_file() -> None:
    """Mirror logs into the data dir.

    A bundled app has no terminal, so without this a crash leaves nothing
    behind but a system crash report.
    """
    path = settings.log_dir / "desktop.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except OSError:
        pass


def cmd_desktop(args: argparse.Namespace) -> int:
    from kestrel.db.migrate import init_db

    settings.ensure_dirs()
    _log_to_file()
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

    if getattr(args, "browser", False):
        # Escape hatch: no webview at all. Used when the native window cannot
        # start on a machine, and handy for debugging.
        import webbrowser

        print(f"Kestrel serving on {url}  (Ctrl+C to stop)")
        webbrowser.open(url)
        try:
            while not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
        return 0

    try:
        import webview  # noqa: F401

        from kestrel.desktop.app import run_desktop
    except ImportError:
        print("Desktop extras are missing. Install them with:")
        print("    uv sync --extra desktop")
        print(f"The web UI is still available at {url}")
        server.should_exit = True
        return 1

    try:
        run_desktop(url, server)
    finally:
        server.should_exit = True
    return 0


def cmd_version(_args: argparse.Namespace) -> int:
    from kestrel.db.migrate import init_db
    from kestrel.services import updates

    # 在空数据目录上直接跑 `kestrel version` 也要能给出答案：读更新缓存需要
    # config_kv 表存在，否则打包版会打印半截信息再抛「no such table」。
    settings.ensure_dirs()
    init_db()

    info = updates.info()
    print(f"Kestrel {info['current']}")
    print(f"  platform : {info['platform']}")
    print(f"  mode     : {info['mode']}")
    print(f"  data dir : {info['home']}")
    print(f"  releases : {info['releases_url']}")
    cached = updates.cached()
    if cached and cached.get("latest"):
        mark = "有更新" if cached.get("available") else "已是最新"
        print(f"  update   : {mark}（远端 {cached['latest'].get('version')}）")
    else:
        print("  update   : 还没检查过，运行 `kestrel update --check`")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    import asyncio

    from kestrel.db.migrate import init_db
    from kestrel.services import updates

    settings.ensure_dirs()
    init_db()

    async def run() -> int:
        if args.install:
            result = await updates.install()
            if not result.get("ok"):
                print(f"安装失败：{result.get('error')}")
                if result.get("url"):
                    print(f"手动下载：{result['url']}")
                return 1
            if result.get("up_to_date"):
                print(f"已经是最新版本 v{updates.info()['current']}。")
                return 0
            print(result.get("next") or "安装包已打开。")
            print(f"文件：{result.get('path')}")
            return 0

        report = await updates.check(force=not args.cached)
        if not report.get("ok"):
            print(f"检查失败：{report.get('error')}")
            return 1
        if not report.get("available"):
            print(f"已是最新版本 v{report.get('current')}。")
            if report.get("reason"):
                print(report["reason"])
            return 0
        latest = report.get("latest") or {}
        print(f"当前 v{report.get('current')} → 最新 v{latest.get('version')}")
        asset = latest.get("asset")
        if asset:
            print(f"安装包：{asset.get('name')}")
            print("运行 `kestrel update --install` 下载并打开。")
        else:
            print(f"这个版本没有当前平台的安装包，请到 {report.get('releases_url')}")
        return 0

    return asyncio.run(run())


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Re-resolve every watchlist symbol whose secid is missing or stale."""
    import asyncio

    from kestrel.datasources import resolve as resolver
    from kestrel.db.engine import connect
    from kestrel.db.migrate import init_db

    settings.ensure_dirs()
    init_db()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, market, code, name, secid FROM symbol ORDER BY market, code"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("自选是空的。先在网页里加几只，再跑这个命令。")
        return 0

    print(f"检查 {len(rows)} 只标的…\n")
    ok = fixed = bad = 0

    async def run() -> None:
        nonlocal ok, fixed, bad
        for row in rows:
            label = f'{row["market"]:2s} {row["code"]:8s} {row["name"]}'
            if row["secid"]:
                ok += 1
                print(f"  ok    {label}  → {row['secid']}")
                continue
            found = await resolver.resolve(row["code"], row["market"])
            if found is None:
                bad += 1
                print(f"  坏     {label}  → 找不到，建议删掉重加")
                continue
            conn2 = connect()
            try:
                conn2.execute(
                    "UPDATE symbol SET secid=?, name=?, exchange=?, resolve_error=NULL"
                    " WHERE id=?",
                    (found.secid, found.name, found.exchange, row["id"]),
                )
                conn2.commit()
            finally:
                conn2.close()
            fixed += 1
            print(f"  修复   {label}  → {found.secid} ({found.name})")

    asyncio.run(run())
    print(f"\n可用 {ok} 只，修复 {fixed} 只，无法识别 {bad} 只。")
    if bad:
        print("无法识别的标的会让扫描跳过它们。删掉重加，或换成正确的代码。")

    # normalise exchange labels that older versions stored as market codes
    conn3 = connect()
    try:
        conn3.execute("UPDATE symbol SET exchange='上交所' WHERE market='CN' AND secid LIKE '1.%'")
        conn3.execute("UPDATE symbol SET exchange='深交所' WHERE market='CN' AND secid LIKE '0.%'")
        conn3.execute("UPDATE symbol SET exchange='港交所' WHERE market='HK' AND secid IS NOT NULL")
        conn3.commit()
    finally:
        conn3.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kestrel", description="Kestrel trading companion")
    parser.add_argument("--version", action="version", version=f"kestrel {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)

    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="initialise the local database")
    p_init.set_defaults(func=cmd_init)

    p_doc = sub.add_parser("doctor", help="re-resolve watchlist symbols that fail to load")
    p_doc.set_defaults(func=cmd_doctor)

    p_version = sub.add_parser("version", help="show version, platform and update state")
    p_version.set_defaults(func=cmd_version)

    p_update = sub.add_parser("update", help="check for a newer release")
    p_update.add_argument("--check", action="store_true", help="force a fresh check")
    p_update.add_argument("--cached", action="store_true", help="use the cached result")
    p_update.add_argument("--install", action="store_true", help="download and open the installer")
    p_update.set_defaults(func=cmd_update)

    p_serve = sub.add_parser("serve", help="run the local web server")
    p_serve.add_argument("--host", default=settings.host)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    p_desk = sub.add_parser("desktop", help="run the desktop client (window + tray)")
    p_desk.add_argument("--port", type=int, default=None)
    p_desk.add_argument(
        "--browser", action="store_true", help="skip the native window, open the system browser"
    )
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
