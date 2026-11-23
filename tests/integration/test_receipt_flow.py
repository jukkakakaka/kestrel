"""Integration: recommendation -> receipt page -> receipt recorded.

Runs against a temporary database so the developer's data is untouched.
"""

from __future__ import annotations

import json
import re

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

    client.post("/api/settings", json={"items": {"scan.markets": ["HK"]}})
    page = client.get("/scan")
    assert page.status_code == 200
    assert "00700" in page.text

    receipt = client.get(f"/r/{code}")
    assert receipt.status_code == 200
    assert "腾讯控股" in receipt.text

    key = re.search(r'name="request_id" value="([^"]+)"', receipt.text)[1]
    incomplete = {"action": "partial", "fill_price": "402.5", "request_id": key}
    rejected = client.post(f"/r/{code}", data=incomplete)
    assert "实际成交价格和数量" in rejected.text

    fill = {**incomplete, "fill_qty": "60", "fees": "2.5", "filled_at": "2025-10-03"}
    for _ in range(2):
        submitted = client.post(f"/r/{code}", data=fill)
        assert submitted.status_code == 200
        assert "成交回执已记录" in submitted.text or "交易台账" in submitted.text

    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM receipt WHERE rec_id=? ORDER BY id DESC LIMIT 1", (rec_id,)
        ).fetchone()
        assert row is not None
        assert row["action"] == "partial"
        assert row["fill_qty"] == 60
        assert conn.execute("SELECT COUNT(*) FROM receipt").fetchone()[0] == 1
        assert conn.execute("SELECT uses FROM receipt_token").fetchone()[0] == 1
        trade = conn.execute("SELECT qty, entry_price, fees, opened_at FROM trade").fetchone()
        assert tuple(trade) == (60, 402_500_000, 2_500_000, "2025-10-02T16:00:00+00:00")
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


def test_market_scope_and_late_fills_are_independent(client):
    rec_id, _code = _seed_recommendation()
    assert client.post("/api/onboarding", json={"skip": True}).json()["ok"]
    assert {m["market"] for m in client.get("/api/market-status").json()["markets"]} == {"CN", "US"}
    assert f'id="rec-{rec_id}"' not in client.get("/advice").text
    assert client.post("/api/settings", json={"items": {"scan.markets": ["HK", "US"]}}).json()["ok"]
    assert f'id="rec-{rec_id}"' in client.get("/advice").text
    assert client.post(f"/advice/{rec_id}/decide", json={"decision": "executed"}).json()["ok"] is False
    body = {"code": "HK/00700", "side": "buy", "price": "402", "qty": "60", "rec_id": rec_id}
    assert client.post("/api/journal/fill", json={**body, "request_id": "first-lot"}).json()["ok"]
    assert client.post(f"/advice/{rec_id}/decide", json={"decision": "skipped"}).json()["ok"] is False
    body.update(price="404", qty="40", request_id="second-lot")
    assert client.post("/api/journal/fill", json=body).json()["ok"]
    assert client.post("/api/journal/fill", json={**body, "qty": "50"}).json()["ok"] is False
    conn = connect()
    try:
        row = conn.execute("SELECT status, fill_qty, fill_price FROM recommendation WHERE id=?", (rec_id,)).fetchone()
        assert tuple(row) == ("executed", 100, 402_800_000)
        assert conn.execute("SELECT COUNT(*) FROM trade").fetchone()[0] == 2
    finally:
        conn.close()
