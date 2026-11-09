"""The quick-simulation page has to render both its empty and its refusal states.

Both POST branches used to crash: the context builder was called without the
request, and then with a duplicate ``result`` keyword. A page that 500s on submit
is not a subtle bug — the feature simply did not exist — so it gets a test now.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kestrel.app import create_app
from kestrel.db.migrate import init_db
from kestrel.settings import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home", tmp_path)
    init_db()
    with TestClient(create_app()) as test_client:
        yield test_client


def test_sim_page_renders(client):
    response = client.get("/sim")
    assert response.status_code == 200
    assert "快速模拟" in response.text
    # the horizon control has to be there, or weekly backtests are unreachable
    assert 'name="horizon"' in response.text


def test_sim_without_symbol_refuses_instead_of_crashing(client):
    response = client.post("/sim", data={"symbol": "", "strategy": "trend_pullback"})
    assert response.status_code == 200
    assert "先选一只标的" in response.text


def test_sim_with_unknown_symbol_refuses_instead_of_crashing(client):
    response = client.post(
        "/sim",
        data={"symbol": "US/ZZZZ", "strategy": "trend_pullback", "horizon": "medium"},
    )
    assert response.status_code == 200
    assert "找不到这只标的" in response.text
