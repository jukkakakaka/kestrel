"""SQLite 数据库连接与事务辅助工具。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kestrel.settings import settings


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开一个启用 WAL、外键和 30 秒锁超时的 SQLite 连接。"""
    db_path = path or settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 与 connect(..., timeout=30.0) 对齐，避免大批量写 K 线时并发请求 5 秒即锁超时
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """在 isolation_level=None 模式下提供安全的显式事务边界。"""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """带自动提交/回滚和自动关闭的连接上下文管理器。"""
    conn = connect(path)
    try:
        with transaction(conn):
            yield conn
    finally:
        conn.close()


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()
