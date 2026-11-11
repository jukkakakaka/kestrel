"""版本、检查更新与自动更新。

更新这件事在新手产品里只有一种正确做法：**告诉他有没有新版本，并替他做完。**
让人自己去 Releases 页面找 dmg、比对版本号、解压拖拽，是开源软件的常态，
但也是新手放弃的第一步。

这里的实现刻意分开三件事：

* **检查**：只读 GitHub Releases 的公开 API，不需要 token，失败不影响主流程；
* **判断**：语义化版本比较，遇到预发布版本或格式不对的标签一律当作"不更新"；
* **安装**：把安装包下载到数据目录，然后**交给系统打开**，而不是在进程里替换自己。

最后一条是硬约束：一个正在运行的 .app 无法安全地覆盖自身，Docker 容器里也不能。
所以"自动更新"在这里的定义是：自动检查 + 自动下载 + 自动打开安装器，
剩下的那一次点击（拖进 Applications / 重启）必须留给用户。
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from kestrel import __version__
from kestrel.settings import is_frozen, settings

log = logging.getLogger(__name__)

REPO = "jukkakakaka/kestrel"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
CACHE_KEY = "update.latest"
CHECK_INTERVAL = timedelta(hours=12)

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


@dataclass(slots=True)
class Release:
    version: str
    tag: str
    url: str
    notes: str
    published_at: str
    assets: list[dict]

    def asset_for_platform(self) -> dict | None:
        """挑出当前平台该用的那个文件。

        Releases 里既有 dmg、也有 Windows zip。选错平台会下载一个打不开的文件，
        所以宁可返回 None——页面会说"去 Releases 手动下载"，而不是给个假按钮。
        """
        names = []
        if sys.platform == "darwin":
            names = [".dmg"]
        elif os.name == "nt":
            names = [".zip", ".exe"]
        else:
            names = [".zip", ".tar.gz"]
        for suffix in names:
            for asset in self.assets:
                if str(asset.get("name", "")).lower().endswith(suffix):
                    return asset
        return None

    def as_dict(self) -> dict:
        asset = self.asset_for_platform()
        return {
            "version": self.version,
            "tag": self.tag,
            "url": self.url,
            "notes": self.notes[:4000],
            "published_at": self.published_at,
            "asset": asset,
        }


def _parse(version: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(str(version).strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(candidate: str, current: str = __version__) -> bool:
    """严格比较，预发布标签（v1.2.0-beta.1）不算更新。"""
    new, old = _parse(candidate), _parse(current)
    if new is None or old is None:
        return False
    return new > old


def platform_label() -> str:
    if sys.platform == "darwin":
        machine = platform.machine()
        return "macOS (Apple Silicon)" if machine == "arm64" else "macOS (Intel)"
    if os.name == "nt":
        return "Windows"
    return "Linux"


def mode_label() -> str:
    """这个版本是怎么跑起来的——决定更新能不能自己装。"""
    if os.environ.get("KESTREL_IN_CONTAINER"):
        return "docker"
    if is_frozen():
        return "app"
    return "source"


async def fetch_latest(timeout: float = 8.0) -> dict:
    """查 GitHub 上最新一个正式版本。没有发布过任何 release 时返回 available=False。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Kestrel/{__version__}",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            resp = await client.get(LATEST_API)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"连不上更新服务器：{exc}"}

    if resp.status_code == 404:
        # 仓库还没发过版本，这不是错误，是「暂时没有可更新的东西」。
        return {
            "ok": True,
            "available": False,
            "current": __version__,
            "reason": "还没有发布过正式版本。",
            "releases_url": RELEASES_URL,
        }
    if resp.status_code >= 300:
        return {"ok": False, "error": f"更新服务器返回 {resp.status_code}"}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": "更新服务器返回的不是 JSON"}

    tag = str(data.get("tag_name") or "")
    release = Release(
        version=tag.lstrip("v"),
        tag=tag,
        url=str(data.get("html_url") or RELEASES_URL),
        notes=str(data.get("body") or ""),
        published_at=str(data.get("published_at") or ""),
        assets=[a for a in (data.get("assets") or []) if isinstance(a, dict)],
    )
    available = is_newer(release.version)
    return {
        "ok": True,
        "available": available,
        "current": __version__,
        "latest": release.as_dict(),
        "releases_url": RELEASES_URL,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "can_self_install": mode_label() in ("app", "source"),
    }


def cached() -> dict | None:
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service

    conn = connect()
    try:
        return config_service.get(conn, CACHE_KEY, None)
    finally:
        conn.close()


def _store(report: dict) -> dict:
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service

    conn = connect()
    try:
        config_service.put(conn, CACHE_KEY, report)
        conn.commit()
    finally:
        conn.close()
    return report


def should_check(report: dict | None) -> bool:
    if not report or not report.get("checked_at"):
        return True
    try:
        moment = datetime.fromisoformat(str(report["checked_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(UTC) - moment > CHECK_INTERVAL


async def check(force: bool = False) -> dict:
    """检查更新并把结果缓存起来。网络失败时返回上一次的结果，而不是清空它。"""
    previous = cached()
    if not force and previous and not should_check(previous):
        return previous | {"cached": True}

    report = await fetch_latest()
    if not report.get("ok") and previous:
        return previous | {"cached": True, "stale": True, "error": report.get("error")}
    if report.get("ok"):
        report["auto"] = auto_enabled()
        _store(report)
    return report


def auto_enabled() -> bool:
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service

    conn = connect()
    try:
        value = config_service.get(conn, "update.auto_check", True)
    finally:
        conn.close()
    return bool(value)


def auto_install_enabled() -> bool:
    from kestrel.db.engine import connect
    from kestrel.services import config as config_service

    conn = connect()
    try:
        value = config_service.get(conn, "update.auto_install", False)
    finally:
        conn.close()
    return bool(value)


def downloads_dir() -> Path:
    path = settings.home / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def download(asset: dict) -> dict:
    """下载安装包到数据目录。返回本地路径，交给调用方决定怎么打开。"""
    name = str(asset.get("name") or "kestrel-update")
    url = str(asset.get("browser_download_url") or "")
    size = int(asset.get("size") or 0)
    if not url:
        return {"ok": False, "error": "这个版本没有提供下载地址"}

    target = downloads_dir() / name
    if target.exists() and size and target.stat().st_size == size:
        return {"ok": True, "path": str(target), "name": name, "cached": True}

    tmp = target.with_suffix(target.suffix + ".part")
    try:
        async with (
            httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 300:
                return {"ok": False, "error": f"下载失败：HTTP {resp.status_code}"}
            with tmp.open("wb") as handle:
                async for chunk in resp.aiter_bytes():
                    handle.write(chunk)
        tmp.replace(target)
        return {"ok": True, "path": str(target), "name": name, "cached": False}
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        return {"ok": False, "error": f"下载失败：{exc}"}


def open_installer(path: str | Path) -> dict:
    """用系统默认程序打开安装包。

    这里不做进程内替换：正在运行的 .app 不能覆盖自己，容器里也不能。
    打开 dmg/zip 让用户完成最后一步，是唯一在所有平台上都正确的做法。
    """
    target = Path(path)
    if not target.exists():
        return {"ok": False, "error": "文件不存在，先下载"}

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])  # noqa: S603
        elif os.name == "nt":
            os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(target)])  # noqa: S603
        return {"ok": True, "opened": str(target), "manual": True}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"打不开安装包：{exc}",
            "path": str(target),
        }


async def install() -> dict:
    """下载并打开安装器。由设置页的「立即更新」按钮调用。"""
    mode = mode_label()
    if mode == "docker":
        return {
            "ok": False,
            "error": (
                "这一份跑在容器里，更新应该由镜像重新拉取完成："
                "docker compose pull && docker compose up -d"
            ),
        }
    report = await check(force=True)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error") or "检查更新失败"}
    if not report.get("available"):
        return {"ok": True, "up_to_date": True, "current": __version__}

    latest = report.get("latest") or {}
    asset = latest.get("asset")
    if not asset:
        return {
            "ok": False,
            "error": f"这个版本没有为 {platform_label()} 提供安装包，请到 Releases 手动下载。",
            "url": latest.get("url") or RELEASES_URL,
        }
    downloaded = await download(asset)
    if not downloaded.get("ok"):
        return downloaded
    opened = open_installer(downloaded["path"])
    if not opened.get("ok"):
        return opened
    return {
        "ok": True,
        "version": latest.get("version"),
        "path": downloaded["path"],
        "next": (
            "安装包已经打开。把 Kestrel 拖进 Applications 覆盖旧版本，然后重新打开即可。"
            if mode == "app"
            else "安装包已经下载并打开。源码运行的话，建议改用 git pull 更新。"
        ),
    }


def info() -> dict:
    """给设置页和 CLI 用的一份完整描述。"""
    return {
        "current": __version__,
        "platform": platform_label(),
        "mode": mode_label(),
        "python": platform.python_version(),
        "frozen": is_frozen(),
        "home": str(settings.home),
        "releases_url": RELEASES_URL,
        "data_dir": str(settings.home),
        "config_dir": str(settings.home),
        "db_path": str(settings.db_path),
    }
