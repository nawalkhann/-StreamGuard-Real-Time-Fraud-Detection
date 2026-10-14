from streamguard import rules
from streamguard.features import FeatureStore, Txn

HR = frozenset({"7995"})


def txn(**kw):
    d = dict(id="x", account="a", amount=10.0, merchant="m", mcc="5411", country="US", channel="online", ts=0)
    d.update(kw)
    return Txn(**d)


def feats(**over):
    f = FeatureStore(HR).compute(txn())
    f.update(over)
    return f


def codes(hits):
    return {h.code for h in hits}


def test_quiet_transaction_has_no_hits():
    assert rules.evaluate(txn(), feats(), frozenset(), 25_000) == []


def test_blocklist_is_hard():
    hits = rules.evaluate(txn(device="bad"), feats(), frozenset({("device", "bad")}), 25_000)
    assert hits[0].code == "BLOCKLIST" and hits[0].hard


def test_card_testing_and_velocity():
    assert "CARD_TESTING" in codes(rules.evaluate(txn(), feats(n_small_10m=3), frozenset(), 25_000))
    assert "VELOCITY_1M" in codes(rules.evaluate(txn(), feats(n_1m=4), frozenset(), 25_000))


def test_amount_ceiling():
    assert "AMOUNT_CEILING" in codes(rules.evaluate(txn(amount=30_000), feats(), frozenset(), 25_000))


def test_combine_is_noisy_or():
    h = [rules.RuleHit("a", "", 0.5), rules.RuleHit("b", "", 0.5)]
    assert abs(rules.combine(h) - 0.75) < 1e-9 and rules.combine([]) == 0
