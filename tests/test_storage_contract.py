"""Both storage backends must behave identically."""
import pytest

from infra import PG_URL, reset_pg
from streamguard.storage import DuplicateTransaction, Storage


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="no PG"))])
def db(request, tmp_path):
    if request.param == "sqlite":
        s = Storage(str(tmp_path / "c.db"))
    else:
        from streamguard.storage_pg import PostgresStorage
        reset_pg()
        s = PostgresStorage(PG_URL)
    yield s
    s.close()


def payload(i, account="a1", amount=10.0, ts=None):
    return {"transaction_id": f"t{i}", "account_id": account, "amount": amount, "currency": "USD",
            "merchant_id": f"m{i}", "merchant_category": "5411", "country": "US", "channel": "online",
            "timestamp": "2026-01-01T00:00:00+00:00"}


def result(i, decision="APPROVE", risk=1.0, latency=1.0):
    return {"transaction_id": f"t{i}", "decision": decision, "risk_score": risk, "ml_score": None,
            "rule_score": 0.0, "reasons": [{"code": "X", "description": "why", "weight": 0.5}] if decision != "APPROVE" else [],
            "model_version": None, "latency_ms": latency, "scored_at": "2026-01-01T00:00:00+00:00", "duplicate": False}


def test_roundtrip_and_duplicate_rejected(db):
    assert db.save_decision(payload(1), 100.0, result(1), alert=False) is None
    assert db.get_result("t1")["decision"] == "APPROVE"
    got = db.get_transaction("t1")
    assert got["transaction"]["account_id"] == "a1" and got["decision"]["transaction_id"] == "t1"
    with pytest.raises(DuplicateTransaction):
        db.save_decision(payload(1), 100.0, result(1), alert=True)
    assert db.list_alerts(None, 10, 0) == []          # failed duplicate must not leave an alert behind
    assert db.get_result("nope") is None and db.get_transaction("nope") is None


def test_alert_shape_listing_filter_and_update(db):
    a1 = db.save_decision(payload(1), 1.0, result(1, "REVIEW", 60), alert=True)
    a2 = db.save_decision(payload(2, amount=99.5), 2.0, result(2, "DECLINE", 99), alert=True)
    assert set(a1) == {"id", "transaction_id", "account_id", "decision", "risk_score", "status", "analyst", "note",
                       "created_at", "updated_at", "amount", "currency", "merchant_id", "country", "reasons"}
    assert a2["amount"] == 99.5 and a2["status"] == "open" and a2["reasons"][0]["code"] == "X"
    assert [a["transaction_id"] for a in db.list_alerts(None, 10, 0)] == ["t2", "t1"]      # newest first
    assert [a["transaction_id"] for a in db.list_alerts(None, 1, 1)] == ["t1"]            # pagination
    up = db.update_alert(a1["id"], "confirmed_fraud", "ana", "n")
    assert (up["status"], up["analyst"], up["note"]) == ("confirmed_fraud", "ana", "n")
    assert [a["transaction_id"] for a in db.list_alerts("open", 10, 0)] == ["t2"]
    assert db.update_alert(99999, "false_positive", "x", None) is None


def test_blocklist_crud_and_upsert(db):
    db.block("device", "d1", "r1")
    db.block("device", "d1", "r2")                       # upsert, not duplicate
    db.block("account", "a9", None)
    rows = db.blocklist()
    assert {(r["kind"], r["value"], r["reason"]) for r in rows} == {("device", "d1", "r2"), ("account", "a9", None)}
    assert db.unblock("device", "d1") is True and db.unblock("device", "d1") is False


def test_history_queries(db):
    db.save_decision(payload(1, "a1"), 300.0, result(1), alert=False)
    db.save_decision(payload(2, "a2"), 100.0, result(2), alert=False)
    db.save_decision(payload(3, "a1"), 200.0, result(3, "REVIEW", 55), alert=True)
    assert [p["transaction_id"] for p in db.history_payloads(0)] == ["t2", "t3", "t1"]
    assert [p["transaction_id"] for p in db.history_payloads(250)] == ["t1"]
    assert [p["transaction_id"] for p in db.account_history("a1", 0)] == ["t3", "t1"]
    assert [p["transaction_id"] for p in db.account_history("a1", 250)] == ["t1"]
    assert db.max_ts() == 300.0
    lab = {p["transaction_id"]: (st, dec) for p, _, st, dec in db.labelled_history()}
    assert lab == {"t1": (None, "APPROVE"), "t2": (None, "APPROVE"), "t3": ("open", "REVIEW")}


def test_stats(db):
    for i, (dec, lat) in enumerate([("APPROVE", 1.0), ("APPROVE", 2.0), ("REVIEW", 3.0), ("DECLINE", 4.0)], 1):
        db.save_decision(payload(i), float(i), result(i, dec, 50, lat), alert=dec != "APPROVE")
    s = db.stats("2000-01-01T00:00:00+00:00")
    assert s["total"] == 4 and s["decisions"] == {"APPROVE": 2, "REVIEW": 1, "DECLINE": 1}
    assert s["open_alerts_total"] == 2 and s["alerts_by_status"] == {"open": 2}
    assert s["latency_ms"]["p50"] in (2.0, 3.0) and s["latency_ms"]["p99"] == 4.0
    assert db.stats("2999-01-01T00:00:00+00:00")["total"] == 0
    assert db.stats("2999-01-01T00:00:00+00:00")["latency_ms"] == {"p50": None, "p95": None, "p99": None}
