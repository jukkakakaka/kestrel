"""CLI 在空数据目录上也要能用。

打包版第一次运行往往就是 `Kestrel version` 或 `Kestrel init`，此时数据库还不
存在。早先 `version` 会先读更新缓存，直接抛「no such table: config_kv」——
信息打印了一半，退出码却还是 0，看起来像正常输出。
"""

from __future__ import annotations

import pytest

from kestrel.cli import main
from kestrel.db.engine import connect
from kestrel.settings import settings


@pytest.fixture()
def empty_home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home", tmp_path / "fresh")
    return tmp_path / "fresh"


def test_version_works_before_the_database_exists(empty_home, capsys):
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert "Kestrel " in out
    assert "data dir" in out
    # 顺带把数据库建好：下一次启动不必再等迁移。
    assert (empty_home / "kestrel.db").exists()


def test_init_is_idempotent(empty_home, capsys):
    assert main(["init"]) == 0
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "migrations applied: 0" in out, "第二次 init 不应重复应用迁移"


def test_the_broker_capability_columns_survive_init(empty_home, capsys):
    assert main(["init"]) == 0
    conn = connect()
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(broker_plan)")}
    finally:
        conn.close()
    assert {"odd_lot_buy", "fractional_us", "fractional_min"} <= cols
