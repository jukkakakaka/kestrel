"""备份导入只接受真实存在的列，且如实报告写入了多少行。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    from kestrel.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_a_known_column_is_imported(client):
    body = {
        "tables": {
            "symbol": [
                {"market": "HK", "code": "00700", "name": "腾讯控股", "currency": "HKD", "lot_size": 100}
            ]
        }
    }
    result = client.post("/api/backup/import", json=body).json()
    assert result["ok"] is True
    assert result["rows"] == 1


def test_an_unknown_column_cannot_break_the_statement(client):
    """列名来自上传文件，不能直接拼进 SQL。"""
    body = {
        "tables": {
            "symbol": [
                {
                    "market": "HK", "code": "00005", "name": "汇丰", "currency": "HKD",
                    "lot_size": 400,
                    "name) VALUES ('x'); DROP TABLE trade; --": "boom",
                }
            ]
        }
    }
    result = client.post("/api/backup/import", json=body).json()
    assert result["ok"] is True
    assert result["rows"] == 1

    conn = connect()
    try:
        # trade 表还在，说明注入没有生效。
        conn.execute("SELECT COUNT(*) FROM trade").fetchone()
        row = conn.execute("SELECT name FROM symbol WHERE code='00005'").fetchone()
    finally:
        conn.close()
    assert row["name"] == "汇丰"


def test_duplicates_are_counted_as_skipped_not_added(client):
    body = {
        "tables": {
            "symbol": [
                {"market": "HK", "code": "00700", "name": "腾讯控股", "currency": "HKD", "lot_size": 100}
            ]
        }
    }
    assert client.post("/api/backup/import", json=body).json()["rows"] == 1
    again = client.post("/api/backup/import", json=body).json()
    assert again["ok"] is True
    assert again["rows"] == 0
    assert again["skipped"] == 1
