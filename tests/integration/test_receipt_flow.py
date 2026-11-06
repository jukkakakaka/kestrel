"""Integration: recommendation -> receipt page -> receipt recorded.

Runs against a temporary database so the developer's data is untouched.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from kestrel.core.money import to_micro
from kestrel.db.engine import connect
from kestrel.db.migrate import init_db
from kestrel.receipts import tokens
from kestrel.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    from kestrel.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _seed_recommendation() -> tuple[int, str]:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO symbol (market, code, name, currency, lot_size)"
            " VALUES ('HK','00700','腾讯控股','HKD',100)"
        )
        symbol_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        cur = conn.execute(
            """
            INSERT INTO recommendation
                (symbol_id, market, code, name, currency, strategy, score,
                 entry_low, entry_high, stop, target1, qty, lot_size,
                 risk_amount, est_fees, breakeven, r_multiple, confidence,
                 valid_until, invalidation, explain_zh, explain_en)
            VALUES (?, 'HK','00700','腾讯控股','HKD','breakout_20',0.72,
                    ?,?,?,?,100,100,?,?,?,3.0,0.8,?,?,?,?)
            """,
            (
                symbol_id,
                to_micro("400.0"), to_micro("404.0"), to_micro("380.0"),
                to_micro("440.0"), to_micro("200.0"), to_micro("18.4"),
                to_micro("400.184"), "2030-01-01T08:00:00+00:00",
                json.dumps(["close_below_stop"]), "测试说明", "test explain",
            ),
        )
        rec_id = cur.lastrowid
        code = tokens.make_code(rec_id)
        conn.execute(
            "INSERT INTO receipt_token (code, rec_id, expires_at) VALUES (?,?,?)",
            (code, rec_id, "2030-01-01T00:00:00+00:00"),
        )
        conn.commit()
        return rec_id, code
    finally:
        conn.close()


def test_receipt_flow(client):
    rec_id, code = _seed_recommendation()

    page = client.get("/scan")
    assert page.status_code == 200
    assert "00700" in page.text

    receipt = client.get(f"/r/{code}")
    assert receipt.status_code == 200
    assert "腾讯控股" in receipt.text

    submitted = client.post(
        f"/r/{code}", data={"action": "executed", "fill_price": "402.5", "note": "ok"}
    )
    assert submitted.status_code == 200

    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM receipt WHERE rec_id=? ORDER BY id DESC LIMIT 1", (rec_id,)
        ).fetchone()
        assert row is not None
        assert row["action"] == "executed"
        assert row["deviation_pct"] is not None
        assert row["deviation_pct"] > 0
        status = conn.execute(
            "SELECT status FROM recommendation WHERE id=?", (rec_id,)
        ).fetchone()["status"]
        assert status == "executed"
    finally:
        conn.close()


def test_unknown_receipt_code_is_rejected(client):
    resp = client.get("/r/ZZZZZZZZ")
    assert resp.status_code == 200
    assert "无效" in resp.text or "invalid" in resp.text.lower()
