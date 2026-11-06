"""Tiny forward-only migration runner.

Migrations are plain .sql files in ``kestrel/db/migrations`` named
``0001_xxx.sql``. Applied versions are recorded in ``schema_migrations``.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from kestrel.db.engine import connect
from kestrel.settings import settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


def pending(db_path: Path | None = None) -> list[Path]:
    conn = connect(db_path)
    try:
        done = _applied(conn)
    finally:
        conn.close()
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if f.stem.split("_")[0] not in done]


def migrate(db_path: Path | None = None) -> int:
    """Apply all pending migrations. Returns the number applied."""
    conn = connect(db_path)
    count = 0
    try:
        done = _applied(conn)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem.split("_")[0]
            if version in done:
                continue
            log.info("applying migration %s", path.name)
            sql = path.read_text(encoding="utf-8")
            conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
            count += 1
        log.info("migrations complete, %d applied", count)
    finally:
        conn.close()
    return count


def init_db(db_path: Path | None = None) -> int:
    settings.ensure_dirs()
    return migrate(db_path)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    init_db()
