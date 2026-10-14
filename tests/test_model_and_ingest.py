import dataclasses
import json
import time

import pytest

from conftest import make_txn
from streamguard.config import Settings
from streamguard.engine import Engine
from streamguard.ingest import process_message
from streamguard.model import FraudModel
from streamguard.schemas import Transaction
from streamguard.storage import Storage
from streamguard.train import train


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    d = tmp_path_factory.mktemp("m")
    s = Settings(api_keys="k", model_path=str(d / "m.joblib"), db_path=str(d / "x.db"), _env_file=None)
    return s, train(s, accounts=600, days=30, seed=3)


def test_trained_model_beats_chance_on_chronological_holdout(trained):
    _, m = trained
    assert m.metrics["roc_auc"] > 0.95 and m.metrics["review"]["recall"] > 0.6
    assert m.metrics["data"] == "synthetic"


def test_model_roundtrip_and_feature_skew_guard(trained, tmp_path):
    s, m = trained
    assert FraudModel.load(s.model_path).version == m.version
    dataclasses.replace(m, feature_names=m.feature_names[:-1]).save(tmp_path / "bad.joblib")
    assert FraudModel.load(tmp_path / "bad.joblib") is None
    assert FraudModel.load(tmp_path / "missing.joblib") is None


def test_engine_with_model_flags_takeover_pattern(trained, tmp_path):
    s, m = trained
    s = s.model_copy(update={"db_path": str(tmp_path / "e.db")})
    eng = Engine(s, Storage(s.db_path), m)
    now = time.time()
    for i in range(12):  # a normal history for the account
        eng.score(Transaction(**make_txn(i, amount=20 + i % 4, timestamp=now - 86400 + i * 600,
                                         device_id="d1", lat=40.7, lon=-74.0)))
    out = eng.score(Transaction(**make_txn(
        99, amount=2500, channel="online", merchant_id="new", country="NG", merchant_category="7995",
        device_id="evil", lat=6.5, lon=3.4, timestamp=now - 3000)))
    assert out.decision in ("REVIEW", "DECLINE")
    assert out.ml_score is not None and out.model_version == m.version
    assert any(r.code.startswith("ML_") for r in out.reasons)  # analysts get model-derived reasons too


def test_process_message_ok_and_dead_letter(settings):
    eng = Engine(settings, Storage(settings.db_path), None)
    ok, out = process_message(eng, json.dumps(make_txn(1)).encode())
    assert ok and json.loads(out)["decision"] == "APPROVE"
    ok, out = process_message(eng, b'{"not": "a transaction"}')
    assert not ok and "error" in json.loads(out)
    ok, _ = process_message(eng, b"\xff\xfe garbage")
    assert not ok
