"""全局测试夹具。

测试必须在**空环境**下也能通过：CI 的 runner 上没有 `data/kestrel.db`，
而早期版本里有测试直接读真实数据库，导致本地绿、CI 红。

做法是在 pytest 导入任何测试模块之前，把 `KESTREL_HOME` 指向一个临时目录，
并把数据库迁移跑完。单独需要自己的库的测试，仍然可以 monkeypatch
`settings.home` 到 `tmp_path` —— 那属于更细粒度的隔离。
"""

from __future__ import annotations

import os
import tempfile

# 必须在 import kestrel.* 之前执行：Settings 在模块导入时就读取环境变量。
os.environ.setdefault("KESTREL_HOME", tempfile.mkdtemp(prefix="kestrel-tests-"))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _initialised_database():
    """保证整个测试会话有一个迁移完成的库。"""
    from kestrel.db.migrate import init_db
    from kestrel.fees import plans as plan_service
    from kestrel.services import glossary
    from kestrel.services import rules as rule_service

    init_db()
    plan_service.hydrate()
    glossary.seed()
    rule_service.ensure_seeded()
    yield
