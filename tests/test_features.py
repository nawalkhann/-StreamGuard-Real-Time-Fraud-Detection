import math

from streamguard.features import FeatureStore, Txn

HR = frozenset({"7995"})


def t(i, ts, amount=20.0, **kw):
    d = dict(id=f"t{i}", account="a", amount=amount, merchant="m", mcc="5411", country="US",
             channel="card_present", ts=ts)
    d.update(kw)
    return Txn(**d)


def test_velocity_windows_count_only_prior_events():
    s = FeatureStore(HR)
    base = 1_700_000_000.0
    for i in range(5):
        f = s.observe(t(i, base + i * 10))
    assert f["n_1m"] == 4 and f["n_10m"] == 4 and f["n_24h"] == 4
    f = s.compute(t(9, base + 2 * 3600))
    assert f["n_1m"] == f["n_10m"] == f["n_1h"] == 0 and f["n_24h"] == 5


def test_card_testing_counts_small_amounts():
    s = FeatureStore(HR)
    for i in range(4):
        s.observe(t(i, 1000 + i * 5, amount=1.5))
    assert s.compute(t(9, 1030))["n_small_10m"] == 4


def test_impossible_travel_speed():
    s = FeatureStore(HR)
    s.observe(t(1, 0, lat=40.71, lon=-74.01))                        # New York
    f = s.compute(t(2, 3600, lat=51.51, lon=-0.13))                   # London 1h later (~5570 km)
    assert 5400 < math.expm1(f["log_dist_km"]) < 5700
    assert math.expm1(f["log_speed_kmh"]) > 5000


def test_amount_zscore_and_new_entities_need_history():
    s = FeatureStore(HR)
    assert s.compute(t(0, 0, device="d9"))["new_device"] == 0        # no history -> not flagged
    for i in range(10):
        s.observe(t(i, i * 3600, amount=20 + i % 3, device="d1"))
    f = s.compute(t(99, 11 * 3600, amount=5000, device="d2", merchant="zzz", country="NG"))
    assert f["amount_zscore"] > 5 and f["new_device"] == 1 and f["new_merchant"] == 1 and f["country_mismatch"] == 1


def test_out_of_order_arrival_is_sorted_and_not_counted_as_future():
    s = FeatureStore(HR)
    s.observe(t(1, 1000)); s.observe(t(2, 1100)); s.observe(t(3, 1050))
    assert [e.ts for e in s._accounts["a"].events] == [1000, 1050, 1100]
    assert s.compute(t(4, 1060))["n_1m"] == 2                        # 1000 and 1050; 1100 is "future"


def test_lru_eviction_bounds_memory():
    s = FeatureStore(HR, max_accounts=3)
    for i in range(10):
        s.observe(t(i, i, account=f"a{i}"))
    assert len(s) == 3
