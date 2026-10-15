"""Scale-out behaviour against REAL Postgres + Redis (skipped unless the STREAMGUARD_TEST_* URLs are set)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis as redis_lib
from fastapi.testclient import TestClient

from conftest import make_txn
from infra import PG_URL, REDIS_URL, needs_redis, reset_pg, reset_redis
from streamguard.api import create_app
from streamguard.config import Settings
from streamguard.engine import build_engine as _build_engine
from streamguard.feature_store_redis import RedisFeatureStore, loads_state
from streamguard.features import FEATURE_NAMES, FeatureStore, Txn
from streamguard.schemas import Transaction
from streamguard.synthetic import generate

pytestmark = needs_redis


_engines = []


def build_engine(settings):
    e = _build_engine(settings)
    _engines.append(e)
    return e


@pytest.fixture
def cfg(tmp_path):
    reset_pg(); reset_redis()
    yield Settings(api_keys="k1", database_url=PG_URL, redis_url=REDIS_URL, model_path=str(tmp_path / "none"),
                   sanctions_dir=str(tmp_path / "none"), blocklist_refresh_seconds=3600, _env_file=None)
    while _engines:
        e = _engines.pop()
        e.db.close()
        e.redis.close()


def test_config_rejects_redis_without_database():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(redis_url="redis://x", database_url="", _env_file=None)


def test_redis_store_features_identical_to_memory_store(cfg):
    rows = generate(60, 12, seed=5)
    eng = build_engine(cfg)
    mem = FeatureStore(cfg.high_risk_mcc_set, structuring_threshold=cfg.structuring_threshold_usd)
    rs = eng.store
    for t, _, _ in rows:
        expected = mem.observe(t)
        with rs.session(t.account) as s:
            got = s.compute(t)
            s.commit(t)
        assert got == expected, (t.id, {k: (got[k], expected[k]) for k in got if got[k] != expected[k]})
    assert len(rows) > 500


def test_state_rebuilt_from_database_after_redis_flush(cfg):
    eng = build_engine(cfg)
    now = time.time()
    for i in range(4):  # card testing in progress
        eng.score(Transaction(**make_txn(i, amount=1.2, merchant_id=f"m{i}", channel="online", timestamp=now - 60 + i * 5)))
    redis_lib.Redis.from_url(REDIS_URL).flushdb()          # cache lost
    d = eng.score(Transaction(**make_txn(9, amount=1.3, merchant_id="m9", channel="online", timestamp=now - 20)))
    assert any(r.code == "CARD_TESTING" for r in d.reasons)  # history survived via the database


def test_fenced_write_refused_after_lock_lease_expiry(cfg):
    eng = build_engine(cfg)
    store, r = eng.store, redis_lib.Redis.from_url(REDIS_URL)
    t = Txn("t1", "acct-x", 10.0, "m", "5411", "US", "online", time.time())
    with store.session("acct-x") as s:
        s.compute(t)
        r.set("sg:lock:acct-x", "another-instance-token")   # our lease "expired" and someone else took over
        s.commit(t)
    assert r.get("sg:acct:acct-x") is None                   # stale write refused, cache invalidated
    assert r.get("sg:lock:acct-x") == b"another-instance-token"  # and we did not steal their lock


def test_exactly_once_and_no_lost_updates_across_two_instances(cfg):
    a, b = build_engine(cfg), build_engine(cfg)               # two independent "instances"
    now = time.time()
    txns = [Transaction(**make_txn(i, amount=20.0 + i, merchant_id=f"m{i % 5}", timestamp=now - 600 + i * 10))
            for i in range(40)]
    jobs = [(eng, t) for t in txns for eng in (a, b)]         # every transaction delivered to BOTH instances
    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(lambda j: j[0].score(j[1]), jobs))
    assert sum(not r.duplicate for r in results) == 40
    assert a.db.stats("1970-01-01T00:00:00+00:00")["total"] == 40
    state = loads_state(redis_lib.Redis.from_url(REDIS_URL).get("sg:acct:acct-1"))
    assert state.n == 40 and len(state.events) == 40          # no lost updates despite 80 concurrent attempts


def test_redis_outage_fails_closed_without_persisting(cfg):
    eng = build_engine(cfg)
    dead = redis_lib.Redis(port=6399, socket_connect_timeout=0.2)
    eng.store = RedisFeatureStore(dead, eng.db, lambda p: None, frozenset())
    with pytest.raises(redis_lib.exceptions.ConnectionError):
        eng.score(Transaction(**make_txn(1)))
    assert eng.db.get_result("tx-1") is None                  # never scored with empty history, never recorded


def _eventually(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def test_alerts_and_blocklist_propagate_between_instances(cfg):
    with TestClient(create_app(cfg), headers={"X-API-Key": "k1"}) as A, \
         TestClient(create_app(cfg), headers={"X-API-Key": "k1"}) as B:
        assert A.get("/readyz").json()["feature_store"] == "redis"
        assert A.get("/readyz").json()["database"] == "postgres"
        # blocklist added on A is enforced on B (pub/sub invalidation)
        A.post("/v1/blocklist", json={"kind": "account", "value": "acct-1"})
        d = _eventually(lambda: (lambda x: x if x["decision"] == "DECLINE" else None)(
            B.post("/v1/transactions/score", json=make_txn(1)).json()))
        assert d and d["decision"] == "DECLINE"
        # an alert raised on A is streamed to a dashboard connected to B
        with B.websocket_connect("/ws/alerts?api_key=k1") as ws:
            A.post("/v1/transactions/score", json=make_txn(2, transaction_id="tx-xinst"))
            assert ws.receive_json()["transaction_id"] == "tx-xinst"
        # analyst action on B is visible from A (shared database)
        alert = A.get("/v1/alerts").json()[0]
        B.patch(f"/v1/alerts/{alert['id']}", json={"status": "confirmed_fraud", "analyst": "ana"})
        assert A.get("/v1/alerts?status=confirmed_fraud").json()[0]["id"] == alert["id"]
