"""Application settings.

All runtime configuration is stored in the database and edited through the web UI.
This module only holds bootstrap values (paths, host, port) that must be known
before the database exists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Directory that holds bundled data files (templates, locales, assets)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent.parent


def _system_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Kestrel"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Kestrel"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "kestrel"


def _default_home() -> Path:
    env = os.environ.get("KESTREL_HOME")
    if env:
        return Path(env).expanduser().resolve()
    # A bundled app is launched from Finder/Explorer, where the working directory
    # is "/" — creating ./data there fails or lands somewhere the user never sees.
    if is_frozen():
        return _system_home()
    return (Path.cwd() / "data").resolve()


class Settings(BaseSettings):
    """Bootstrap settings, overridable with KESTREL_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="KESTREL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    home: Path = Field(default_factory=_default_home)
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    @property
    def db_path(self) -> Path:
        return self.home / "kestrel.db"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.home / "backups"

    @property
    def secret_key_path(self) -> Path:
        return self.home / "secret.key"

    @property
    def lock_path(self) -> Path:
        return self.home / "kestrel.lock"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.cache_dir, self.log_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
