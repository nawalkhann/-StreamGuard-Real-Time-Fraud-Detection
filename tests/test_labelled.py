import time

import pytest

from conftest import make_txn
from streamguard.config import Settings
from streamguard.engine import Engine
from streamguard.labelled import load_csv, load_db, read_labels
from streamguard.schemas import Transaction
from streamguard.storage import Storage
from streamguard.synthetic import generate, write_csv
from streamguard.train import recommend, train_from_rows


@pytest.fixture
def s(tmp_path):
    return Settings(api_keys="k", model_path=str(tmp_path / "m.joblib"), db_path=str(tmp_path / "d.db"), _env_file=None)


def test_csv_roundtrip_trains_a_usable_model(s, tmp_path):
    rows = generate(700, 30, seed=11)
    path = str(tmp_path / "data.csv")
    write_csv(rows, path)
    loaded = load_csv(path, s)
    assert len(loaded) == len(rows) and sum(r[1] for r in loaded) == sum(r[1] for r in rows)
    m = train_from_rows(s, loaded, "csv:test", min_positives=20)
    assert m.metrics["roc_auc"] > 0.9 and m.metrics["data"] == "csv:test"
    assert set(m.metrics["recommended"]) == {"review_threshold", "decline_threshold"}


def test_csv_bad_rows_skipped_and_missing_columns_rejected(s, tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("transaction_id,account_id,amount,merchant_id,merchant_category,country,channel,timestamp,is_fraud\n"
                 "a,x,10,m,5411,US,online,2026-01-01T00:00:00Z,0\n"
                 "b,x,-5,m,5411,US,online,2026-01-01T00:00:00Z,1\n"       # negative amount
                 "c,x,10,m,5411,US,online,2026-01-01T00:01:00Z,maybe\n")  # bad label
    assert [r[0].id for r in load_csv(str(p), s)] == ["a"]
    q = tmp_path / "nocols.csv"
    q.write_text("transaction_id,amount\n1,2\n")
    with pytest.raises(SystemExit):
        load_csv(str(q), s)


def test_refuses_to_train_with_too_few_frauds(s):
    rows = generate(150, 20, seed=5)
    with pytest.raises(SystemExit, match="Refusing to train"):
        train_from_rows(s, rows, "synthetic", min_positives=10_000)


def test_db_labels_from_dispositions_and_chargeback_override(s, tmp_path):
    eng = Engine(s, Storage(s.db_path), None)
    now = time.time()
    eng.db.block("account", "bad", None)
    eng.reload_blocklist()
    eng.score(Transaction(**make_txn(1, timestamp=now - 100)))                      # approve -> assumed legit
    eng.score(Transaction(**make_txn(2, account_id="bad", timestamp=now - 90)))     # decline -> open alert
    eng.score(Transaction(**make_txn(3, account_id="bad", timestamp=now - 80)))     # decline -> will be FP
    alerts = {a["transaction_id"]: a["id"] for a in eng.db.list_alerts(None, 10, 0)}
    eng.db.update_alert(alerts["tx-3"], "false_positive", "ana", None)
    eng.db.close()

    labels = {r[0].id: r[1] for r in load_db(s, None, True)}
    assert labels == {"tx-1": 0, "tx-2": None, "tx-3": 0}
    assert {r[0].id: r[1] for r in load_db(s, None, False)}["tx-1"] is None
    f = tmp_path / "cb.csv"
    f.write_text("transaction_id,is_fraud\ntx-1,1\n")
    assert read_labels(str(f)) == {"tx-1": 1}
    assert {r[0].id: r[1] for r in load_db(s, str(f), True)}["tx-1"] == 1


def test_recommend_picks_lowest_threshold_meeting_precision():
    table = [{"threshold": 0.3, "flagged": 10, "precision": 0.3, "recall": 0.9},
             {"threshold": 0.5, "flagged": 8, "precision": 0.6, "recall": 0.8},
             {"threshold": 0.9, "flagged": 3, "precision": 1.0, "recall": 0.4}]
    assert recommend(table, 0.5, 0.9) == {"review_threshold": 0.5, "decline_threshold": 0.9}
    assert recommend(table, 1.1, 1.1) == {"review_threshold": None, "decline_threshold": None}


def test_recommend_keeps_decline_strictly_above_review():
    table = [{"threshold": 0.2, "flagged": 5, "precision": 1.0, "recall": 1.0},
             {"threshold": 0.3, "flagged": 5, "precision": 1.0, "recall": 1.0}]
    assert recommend(table, 0.5, 0.9) == {"review_threshold": 0.2, "decline_threshold": 0.3}
    assert recommend(table[:1], 0.5, 0.9) == {"review_threshold": 0.2, "decline_threshold": None}
