"""Stateful, per-account streaming features.

The same code computes features for live scoring and for offline training, so
there is no train/serve skew. State is held in memory (bounded LRU) and can be
rebuilt from the transaction log on startup (see ``Engine.warm_start``).
"""
from __future__ import annotations

import bisect
import math
import threading
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field

EARTH_KM = 6371.0088
DAY = 86_400.0
MAX_EVENTS = 500  # per account, within the last 24h
MAX_SET = 64
SMALL_AMOUNT_USD = 5.0
LOCK_STRIPES = 128

FEATURE_NAMES: list[str] = [
    "log_amount", "amount_zscore", "history_len",
    "n_1m", "n_10m", "n_1h", "n_24h", "sum_24h_log", "n_small_10m",
    "log_secs_since_last", "distinct_merchants_1h",
    "new_device", "device_missing", "new_merchant", "country_mismatch",
    "log_dist_km", "log_speed_kmh",
    "hour_sin", "hour_cos", "is_online", "is_atm", "high_risk_mcc",
]


@dataclass(slots=True)
class Txn:
    """Internal, normalised transaction (amount already in USD, ts = epoch seconds)."""
    id: str
    account: str
    amount: float
    merchant: str
    mcc: str
    country: str
    channel: str
    ts: float
    lat: float | None = None
    lon: float | None = None
    device: str | None = None
    ip: str | None = None
    cp_name: str | None = None      # counterparty / beneficiary name (screened, not a model feature)
    cp_country: str | None = None


@dataclass(slots=True)
class _Event:
    ts: float
    amount: float
    merchant: str
    lat: float | None
    lon: float | None

    def __lt__(self, other: "_Event") -> bool:  # for bisect
        return self.ts < other.ts


@dataclass
class AccountState:
    events: list[_Event] = field(default_factory=list)  # sorted by ts, last 24h
    n: int = 0
    mean: float = 0.0  # Welford over log1p(amount), lifetime of the state
    m2: float = 0.0
    devices: set[str] = field(default_factory=set)
    merchants: set[str] = field(default_factory=set)
    countries: Counter = field(default_factory=Counter)
    max_ts: float = 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def compute_features(st: "AccountState", t: Txn, high_risk_mccs: frozenset[str], struct_lo: float,
                     struct_hi: float) -> dict[str, float]:
    """Pure function: features for ``t`` given an account's state. Does not mutate ``st``."""
    ts = t.ts
    n1 = n10 = n60 = n24 = small10 = near_thr = 0
    sum24 = 0.0
    merchants_1h: set[str] = set()
    prev: _Event | None = None
    prev_geo: _Event | None = None

    for e in reversed(st.events):
        dt = ts - e.ts
        if dt < 0:
            continue  # event "from the future" relative to this one (out-of-order arrival)
        if dt > DAY:
            break
        if prev is None:
            prev = e
        if prev_geo is None and e.lat is not None and e.lon is not None:
            prev_geo = e
        n24 += 1
        sum24 += e.amount
        if struct_lo <= e.amount < struct_hi:
            near_thr += 1
        if dt <= 3600:
            n60 += 1
            merchants_1h.add(e.merchant)
            if dt <= 600:
                n10 += 1
                if e.amount < SMALL_AMOUNT_USD:
                    small10 += 1
                if dt <= 60:
                    n1 += 1

    la = math.log1p(t.amount)
    if st.n >= 5:
        var = st.m2 / (st.n - 1)
        z = (la - st.mean) / max(math.sqrt(var), 0.3)
        z = max(-10.0, min(10.0, z))
    else:
        z = 0.0

    history_ok = st.n >= 3
    home = st.countries.most_common(1)[0][0] if history_ok and st.countries else None

    dist = speed = 0.0
    if prev_geo is not None and t.lat is not None and t.lon is not None:
        dist = haversine_km(prev_geo.lat, prev_geo.lon, t.lat, t.lon)
        hours = max(ts - prev_geo.ts, 60.0) / 3600.0
        speed = dist / hours

    secs = (ts - prev.ts) if prev is not None else DAY * 7
    hour = (ts % DAY) / 3600.0
    ang = 2 * math.pi * hour / 24.0

    return {
        "log_amount": la,
        "amount_zscore": z,
        "history_len": math.log1p(st.n),
        "n_1m": float(n1), "n_10m": float(n10), "n_1h": float(n60), "n_24h": float(n24),
        "sum_24h_log": math.log1p(sum24),
        "n_small_10m": float(small10),
        "log_secs_since_last": math.log1p(max(secs, 0.0)),
        "distinct_merchants_1h": float(len(merchants_1h)),
        "new_device": float(history_ok and t.device is not None and t.device not in st.devices),
        "device_missing": float(t.device is None),
        "new_merchant": float(history_ok and t.merchant not in st.merchants),
        "country_mismatch": float(home is not None and t.country != home),
        "log_dist_km": math.log1p(dist),
        "log_speed_kmh": math.log1p(speed),
        "hour_sin": math.sin(ang), "hour_cos": math.cos(ang),
        "is_online": float(t.channel in ("online", "wallet")),
        "is_atm": float(t.channel == "atm"),
        "high_risk_mcc": float(t.mcc in high_risk_mccs),
        # rule-only signals (deliberately NOT in FEATURE_NAMES, so the trained model is unaffected)
        "n_near_threshold_24h": float(near_thr),
        "near_threshold": float(struct_lo <= t.amount < struct_hi),
    }


def apply_txn(st: "AccountState", t: Txn) -> None:
    """Fold ``t`` into an account's state (mutates ``st``)."""
    ev = _Event(t.ts, t.amount, t.merchant, t.lat, t.lon)
    if not st.events or st.events[-1].ts <= ev.ts:
        st.events.append(ev)
    else:
        bisect.insort(st.events, ev)
    st.max_ts = max(st.max_ts, t.ts)
    cutoff = st.max_ts - DAY
    if st.events[0].ts < cutoff or len(st.events) > MAX_EVENTS:
        keep = [e for e in st.events if e.ts >= cutoff]
        st.events = keep[-MAX_EVENTS:]

    x = math.log1p(t.amount)
    st.n += 1
    d = x - st.mean
    st.mean += d / st.n
    st.m2 += d * (x - st.mean)
    if t.device is not None and len(st.devices) < MAX_SET:
        st.devices.add(t.device)
    if len(st.merchants) < MAX_SET * 4:
        st.merchants.add(t.merchant)
    st.countries[t.country] += 1


class FeatureStore:
    def __init__(self, high_risk_mccs: frozenset[str] | set[str], max_accounts: int = 200_000,
                 structuring_threshold: float = 10_000.0):
        self.high_risk_mccs = frozenset(high_risk_mccs)
        self.struct_lo = 0.8 * structuring_threshold
        self.struct_hi = structuring_threshold
        self.max_accounts = max_accounts
        self._accounts: OrderedDict[str, AccountState] = OrderedDict()
        self._meta = threading.Lock()
        self._stripes = [threading.RLock() for _ in range(LOCK_STRIPES)]

    # -- concurrency -------------------------------------------------------
    @contextmanager
    def account_lock(self, account: str):
        lock = self._stripes[hash(account) % LOCK_STRIPES]
        with lock:
            yield

    needs_warm_start = True

    def __len__(self) -> int:
        return len(self._accounts)

    def describe(self) -> dict:
        return {"backend": "memory"}

    def accounts(self) -> int | None:
        return len(self._accounts)

    def _get(self, account: str, create: bool) -> AccountState | None:
        with self._meta:
            st = self._accounts.get(account)
            if st is not None:
                self._accounts.move_to_end(account)
            elif create:
                st = self._accounts[account] = AccountState()
                while len(self._accounts) > self.max_accounts:
                    self._accounts.popitem(last=False)
            return st

    # -- feature computation (read only) -----------------------------------
    def compute(self, t: Txn) -> dict[str, float]:
        st = self._get(t.account, create=False) or AccountState()
        return compute_features(st, t, self.high_risk_mccs, self.struct_lo, self.struct_hi)

    # -- state update ------------------------------------------------------
    def commit(self, t: Txn) -> None:
        apply_txn(self._get(t.account, create=True), t)

    @contextmanager
    def session(self, account: str):
        """Exclusive access to one account: ``with store.session(a) as s: f = s.compute(t); ...; s.commit(t)``."""
        with self.account_lock(account):
            yield self

    def observe(self, t: Txn) -> dict[str, float]:
        """compute + commit in one step (used for offline training/replay)."""
        with self.account_lock(t.account):
            f = self.compute(t)
            self.commit(t)
        return f
