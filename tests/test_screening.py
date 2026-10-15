"""Fixtures below use FICTITIOUS names in the real files' layouts. They test the code, not any real list."""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_txn
from streamguard import rules
from streamguard.api import create_app
from streamguard.config import Settings
from streamguard.features import FeatureStore, Txn
from streamguard.screening import Screener, normalize

SDN = '''100,"ZARKOV, Ivan Petrovich","individual","SDGT",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"remark"
101,"NORTHWIND RELIEF FOUNDATION","-0- ","SDGT",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"remark"
102,"BASHIR","individual","SDGT",-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,-0- ,"single word"
'''
ALT = '100,201,"aka","Ivan P. Zarkoff","-0- "\n'
UN = '''<?xml version="1.0"?><CONSOLIDATED_LIST><INDIVIDUALS>
<INDIVIDUAL><DATAID>1</DATAID><FIRST_NAME>Quentin</FIRST_NAME><SECOND_NAME>Marlowe</SECOND_NAME><REFERENCE_NUMBER>QDi.999</REFERENCE_NUMBER>
<INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Q. Marlo</ALIAS_NAME></INDIVIDUAL_ALIAS></INDIVIDUAL></INDIVIDUALS>
<ENTITIES><ENTITY><DATAID>2</DATAID><FIRST_NAME>Blue Harbor Trading Company</FIRST_NAME><REFERENCE_NUMBER>QDe.998</REFERENCE_NUMBER></ENTITY></ENTITIES></CONSOLIDATED_LIST>'''


@pytest.fixture
def lists(tmp_path):
    d = tmp_path / "san"
    d.mkdir()
    (d / "sdn.csv").write_text(SDN)
    (d / "alt.csv").write_text(ALT)
    (d / "consolidated.xml").write_text(UN)
    (d / "custom_internal.csv").write_text("name,id\nOrion Test Holdings,INT-1\n")
    return d


def test_normalize_folds_accents_and_punctuation():
    assert normalize("  José  O'Brien-Smith, Jr. ") == "jose o brien smith jr"


def test_loads_all_formats_and_reports_sources(lists):
    sc = Screener.from_dir(lists)
    assert set(sc.sources) == {"OFAC_SDN", "UN_CONSOLIDATED", "CUSTOM:custom_internal"}
    m = sc.screen("Ivan Petrovich Zarkov")          # SDN stores "LAST, FIRST"
    assert m and m.exact and m.entry.uid == "100"
    assert sc.screen("Ivan P. Zarkoff").entry.uid == "100"        # alias from alt.csv
    assert sc.screen("Q. Marlo").entry.list_name == "UN_CONSOLIDATED"
    assert sc.screen("Blue Harbor Trading Company").entry.uid == "QDe.998"
    assert sc.screen("orion test holdings").entry.uid == "INT-1"


def test_fuzzy_word_order_typo_and_non_matches(lists):
    sc = Screener.from_dir(lists)
    m = sc.screen("Zarkov Ivan Petrovitch")           # reordered + spelling variant
    assert m and not m.exact and m.score >= 0.9
    assert sc.screen("Ivan Petrov") is None            # different person must not match
    assert sc.screen("Northwind Bakery") is None
    assert sc.screen("Bashir") .exact                  # exact single-word still matches...
    assert sc.screen("Bashar") is None                 # ...but no fuzzy on single words
    assert sc.screen(None) is None and sc.screen("   ") is None


def test_missing_dir_gives_empty_screener(tmp_path):
    assert len(Screener.from_dir(tmp_path / "nope")) == 0


def test_jurisdiction_and_structuring_rules():
    s = FeatureStore(frozenset())
    base = dict(account="a", merchant="m", mcc="5411", country="US", channel="online")
    for i in range(2):
        s.observe(Txn(id=f"p{i}", amount=9_500.0, ts=1000 + i * 60, **base))
    t = Txn(id="x", amount=9_700.0, ts=1200, cp_country="IR", **base)
    codes = {h.code for h in rules.evaluate(t, s.compute(t), frozenset(), 25_000, frozenset({"IR"}))}
    assert {"STRUCTURING", "HIGH_RISK_JURISDICTION"} <= codes
    t2 = Txn(id="y", amount=9_700.0, ts=1200, cp_country="FR", **base)
    assert "HIGH_RISK_JURISDICTION" not in {h.code for h in rules.evaluate(t2, s.compute(t2), frozenset(), 25_000, frozenset({"IR"}))}


@pytest.fixture
def screened_client(lists, tmp_path):
    s = Settings(api_keys="k1", db_path=str(tmp_path / "s.db"), model_path=str(tmp_path / "none"),
                 sanctions_dir=str(lists), _env_file=None)
    with TestClient(create_app(s), headers={"X-API-Key": "k1"}) as c:
        yield c


def test_api_exact_match_declines_fuzzy_goes_to_review_clean_approves(screened_client):
    c = screened_client
    exact = c.post("/v1/transactions/score", json=make_txn(1, counterparty_name="Northwind Relief Foundation")).json()
    assert exact["decision"] == "DECLINE" and exact["reasons"][0]["code"] == "SANCTIONS_MATCH"
    assert "OFAC_SDN" in exact["reasons"][0]["description"]
    fuzzy = c.post("/v1/transactions/score", json=make_txn(2, counterparty_name="Zarkov Ivan Petrovitch")).json()
    assert fuzzy["decision"] == "REVIEW" and fuzzy["reasons"][0]["code"] == "SANCTIONS_MATCH"
    clean = c.post("/v1/transactions/score", json=make_txn(3, counterparty_name="Acme Hardware")).json()
    assert clean["decision"] == "APPROVE"
    assert {a["transaction_id"] for a in c.get("/v1/alerts").json()} == {"tx-1", "tx-2"}


def test_readyz_reports_lists_and_warns_when_missing(screened_client, client):
    r = screened_client.get("/readyz").json()["sanctions"]
    assert r["names"] > 0 and r["warnings"] == []
    assert "no sanctions lists loaded" in client.get("/readyz").json()["sanctions"]["warnings"]
