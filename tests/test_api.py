import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import make_txn
from streamguard.api import create_app
from streamguard.config import Settings


def test_auth_required(client):
    assert client.post("/v1/transactions/score", json=make_txn(), headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/v1/alerts", headers={"X-API-Key": ""}).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_refuses_to_start_without_keys(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(Settings(api_keys="", db_path=str(tmp_path / "x.db"), _env_file=None))


def test_approve_normal_transaction(client):
    r = client.post("/v1/transactions/score", json=make_txn())
    assert r.status_code == 200
    d = r.json()
    assert d["decision"] == "APPROVE" and d["ml_score"] is None and d["latency_ms"] < 250
    assert client.get("/readyz").json()["mode"] == "rules-only"


def test_idempotent_replay_does_not_double_count(client):
    first = client.post("/v1/transactions/score", json=make_txn(5)).json()
    again = client.post("/v1/transactions/score", json=make_txn(5))
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json()["duplicate"] is True and again.json()["scored_at"] == first["scored_at"]
    assert client.get("/v1/stats").json()["total"] == 1


def test_validation_unsupported_currency_and_future_timestamp(client):
    assert client.post("/v1/transactions/score", json=make_txn(amount=-5)).status_code == 422
    assert client.post("/v1/transactions/score", json=make_txn(card_number="4111")).status_code == 422  # extra=forbid
    assert client.post("/v1/transactions/score", json=make_txn(2, currency="XYZ")).status_code == 422
    assert client.post("/v1/transactions/score", json=make_txn(3, timestamp=time.time() + 3600)).status_code == 422


def test_card_testing_burst_is_flagged_and_alerted(client):
    now = time.time()
    last = None
    for i in range(6):
        last = client.post("/v1/transactions/score", json=make_txn(
            100 + i, amount=1.2, merchant_id=f"mm{i}", channel="online", timestamp=now - 60 + i * 5)).json()
    assert last["decision"] in ("REVIEW", "DECLINE")
    assert any(r["code"] == "CARD_TESTING" for r in last["reasons"])
    alerts = client.get("/v1/alerts?status=open").json()
    assert alerts and alerts[0]["transaction_id"] == last["transaction_id"]


def test_alert_disposition_workflow(client):
    client.post("/v1/blocklist", json={"kind": "account", "value": "acct-1", "reason": "test"})
    d = client.post("/v1/transactions/score", json=make_txn(7)).json()
    assert d["decision"] == "DECLINE" and d["reasons"][0]["code"] == "BLOCKLIST"
    alert = client.get("/v1/alerts").json()[0]
    r = client.patch(f"/v1/alerts/{alert['id']}", json={"status": "confirmed_fraud", "analyst": "ana", "note": "x"})
    assert r.json()["status"] == "confirmed_fraud" and r.json()["analyst"] == "ana"
    assert client.get("/v1/alerts?status=open").json() == []
    assert client.patch("/v1/alerts/9999", json={"status": "false_positive", "analyst": "a"}).status_code == 404
    assert client.patch(f"/v1/alerts/{alert['id']}", json={"status": "bogus", "analyst": "a"}).status_code == 422


def test_blocklist_crud_takes_effect_immediately(client):
    client.post("/v1/blocklist", json={"kind": "merchant", "value": "m-1"})
    assert client.post("/v1/transactions/score", json=make_txn(8)).json()["decision"] == "DECLINE"
    assert client.delete("/v1/blocklist/merchant/m-1").status_code == 204
    assert client.post("/v1/transactions/score", json=make_txn(9)).json()["decision"] == "APPROVE"
    assert client.delete("/v1/blocklist/merchant/m-1").status_code == 404


def test_websocket_receives_alerts_and_requires_key(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/alerts?api_key=bad"):
            pass
    client.post("/v1/blocklist", json={"kind": "account", "value": "acct-1"})
    with client.websocket_connect("/ws/alerts?api_key=k1") as ws:
        client.post("/v1/transactions/score", json=make_txn(11))
        assert ws.receive_json()["transaction_id"] == "tx-11"


def test_state_survives_restart(settings):
    now = time.time()
    with TestClient(create_app(settings), headers={"X-API-Key": "k1"}) as c:
        for i in range(5):
            c.post("/v1/transactions/score", json=make_txn(200 + i, timestamp=now - 30 + i))
    with TestClient(create_app(settings), headers={"X-API-Key": "k1"}) as c:
        assert c.get("/readyz").json()["accounts_in_memory"] == 1  # warm start rebuilt feature state
        assert c.post("/v1/transactions/score", json=make_txn(300, timestamp=now)).json()["decision"] == "APPROVE"


def test_concurrent_duplicate_deliveries_score_exactly_once(settings):
    from concurrent.futures import ThreadPoolExecutor

    from streamguard.engine import Engine
    from streamguard.schemas import Transaction
    from streamguard.storage import Storage

    eng = Engine(settings, Storage(settings.db_path), None)
    txn = Transaction(**make_txn(77))
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda _: eng.score(txn), range(32)))
    assert sum(not r.duplicate for r in results) == 1
    assert len({r.scored_at for r in results}) == 1
    assert eng.db.stats("1970-01-01")["total"] == 1
