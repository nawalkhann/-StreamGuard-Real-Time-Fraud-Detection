"""Synthetic card-transaction generator with labelled fraud episodes.

Used (a) to bootstrap a model before real labelled data exists and (b) to drive
the live simulator. It is NOT a substitute for your own labelled history.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .features import Txn

CITIES = [
    ("US", 40.71, -74.01), ("US", 34.05, -118.24), ("US", 41.88, -87.63), ("GB", 51.51, -0.13),
    ("DE", 52.52, 13.40), ("FR", 48.86, 2.35), ("IN", 28.61, 77.21), ("IN", 19.08, 72.88),
    ("AE", 25.20, 55.27), ("SG", 1.35, 103.82), ("BR", -23.55, -46.63), ("NG", 6.52, 3.38),
    ("RU", 55.76, 37.62), ("CN", 31.23, 121.47), ("AU", -33.87, 151.21), ("CA", 43.65, -79.38),
]
MCC_COMMON = ["5411", "5812", "5541", "5311", "5912", "4111", "5732", "5999", "5651", "5814"]
MCC_RISKY = ["4829", "6051", "7995", "5967", "6211", "5816"]
HOUR_W = [1, 1, 1, 1, 1, 2, 4, 7, 9, 9, 9, 10, 11, 10, 9, 9, 9, 10, 11, 11, 9, 6, 3, 2]
FRAUD_TYPES = ("card_testing", "account_takeover", "stolen_card", "amount_spike")


@dataclass
class _Profile:
    acct: str
    home: int
    mu: float
    sigma: float
    rate: float
    devices: list[str]
    merchants: list[str]
    online_share: float


def _jit(rng: random.Random, city: int, spread: float = 0.25) -> tuple[float, float]:
    _, lat, lon = CITIES[city]
    return lat + rng.uniform(-spread, spread), lon + rng.uniform(-spread, spread)


def _mcc_for(merchant: str) -> str:
    return MCC_COMMON[sum(map(ord, merchant)) % len(MCC_COMMON)]


def _pick_time(rng: random.Random, day: int, t0: float) -> float:
    hour = rng.choices(range(24), HOUR_W)[0]
    return t0 + day * 86400 + hour * 3600 + rng.uniform(0, 3600)


def generate(
    n_accounts: int = 2000, days: int = 45, seed: int = 7, start_ts: float = 1_700_000_000.0,
    fraud_account_share: float = 0.06,
) -> list[tuple[Txn, int, str]]:
    """Return ``(txn, label, fraud_type)`` sorted by time. label 1 = fraud."""
    rng = random.Random(seed)
    out: list[tuple[Txn, int, str]] = []
    counter = 0

    def nid() -> str:
        nonlocal counter
        counter += 1
        return f"T{seed}-{counter:08d}"

    profiles: list[_Profile] = []
    for i in range(n_accounts):
        profiles.append(_Profile(
            acct=f"A{seed}-{i:05d}", home=rng.randrange(len(CITIES)),
            mu=rng.uniform(2.6, 4.2), sigma=rng.uniform(0.5, 1.0), rate=rng.uniform(0.4, 3.5),
            devices=[f"D{seed}-{i}-{k}" for k in range(rng.choice([1, 1, 2, 3]))],
            merchants=[f"M{rng.randrange(5000)}" for _ in range(rng.randint(8, 20))],
            online_share=rng.uniform(0.1, 0.7),
        ))

    for p in profiles:
        trip = None
        if rng.random() < 0.15:  # legitimate travel
            s = rng.randrange(max(1, days - 6))
            trip = (s, s + rng.randint(2, 5), rng.choice([c for c in range(len(CITIES)) if c != p.home]))
        for day in range(days):
            for _ in range(_poisson(rng, p.rate)):
                ts = _pick_time(rng, day, start_ts)
                abroad = trip is not None and trip[0] <= day <= trip[1]
                city = trip[2] if abroad else p.home
                online = rng.random() < p.online_share
                amt = math.exp(rng.gauss(p.mu, p.sigma))
                if rng.random() < 0.012:
                    amt *= rng.uniform(5, 15)  # legit big purchase
                merch = rng.choice(p.merchants) if rng.random() < 0.8 else f"M{rng.randrange(5000)}"
                dev = None
                if online:
                    dev = rng.choice(p.devices) if rng.random() > 0.03 else f"D{seed}-{p.acct}-new{rng.randrange(99)}"
                lat, lon = _jit(rng, city)
                out.append((Txn(nid(), p.acct, round(amt, 2), merch, _mcc_for(merch), CITIES[city][0],
                                "online" if online else rng.choice(["card_present", "card_present", "atm"]),
                                ts, lat, lon, dev, None), 0, ""))

    # --- inject fraud episodes -------------------------------------------
    by_acct: dict[str, list[int]] = {}
    for idx, (t, _, _) in enumerate(out):
        by_acct.setdefault(t.account, []).append(idx)
    victims = rng.sample(profiles, int(n_accounts * fraud_account_share))
    horizon = start_ts + days * 86400
    for p in victims:
        for _ in range(rng.choice([1, 1, 2])):
            kind = rng.choice(FRAUD_TYPES)
            foreign = rng.choice([c for c in range(len(CITIES)) if CITIES[c][0] != CITIES[p.home][0]])
            typical = math.exp(p.mu)
            start = start_ts + rng.uniform(7, days - 1) * 86400
            thief_dev = f"D{seed}-fraud-{rng.randrange(10**6)}"
            eps: list[Txn] = []
            if kind == "card_testing":
                city = foreign if rng.random() < 0.5 else p.home
                ts = start
                for _ in range(rng.randint(4, 10)):
                    ts += rng.uniform(5, 60)
                    m = f"M{rng.randrange(5000)}"
                    la, lo = _jit(rng, city)
                    eps.append(Txn(nid(), p.acct, round(rng.uniform(0.5, 4.0), 2), m, _mcc_for(m),
                                   CITIES[city][0], "online", ts, la, lo, thief_dev, None))
                if rng.random() < 0.5:
                    for _ in range(rng.randint(1, 2)):
                        ts += rng.uniform(30, 600)
                        m = f"M{rng.randrange(5000)}"
                        la, lo = _jit(rng, city)
                        eps.append(Txn(nid(), p.acct, round(rng.uniform(200, 1500), 2), m, rng.choice(MCC_RISKY),
                                       CITIES[city][0], "online", ts, la, lo, thief_dev, None))
            elif kind == "account_takeover":
                ts = start
                for _ in range(rng.randint(2, 5)):
                    ts += rng.uniform(300, 5400)
                    m = f"M{rng.randrange(5000)}"
                    la, lo = _jit(rng, foreign)
                    eps.append(Txn(nid(), p.acct, round(max(300, typical * rng.uniform(3, 8)), 2), m,
                                   rng.choice(MCC_RISKY) if rng.random() < 0.6 else _mcc_for(m),
                                   CITIES[foreign][0], "online", ts, la, lo, thief_dev, None))
            elif kind == "stolen_card":
                ts = start + rng.uniform(3600, 4 * 3600)
                la0, lo0 = _jit(rng, p.home)
                m0 = rng.choice(p.merchants)  # a genuine purchase just before the theft
                eps.append(Txn(nid(), p.acct, round(typical, 2), m0, _mcc_for(m0), CITIES[p.home][0],
                               "card_present", start, la0, lo0, None, None))
                legit_leg = eps.pop()  # genuine leg is labelled 0
                out.append((legit_leg, 0, ""))
                for _ in range(rng.randint(1, 4)):
                    ts += rng.uniform(600, 7200)
                    m = f"M{rng.randrange(5000)}"
                    la, lo = _jit(rng, foreign)
                    eps.append(Txn(nid(), p.acct, round(typical * rng.uniform(2, 5), 2), m, _mcc_for(m),
                                   CITIES[foreign][0], rng.choice(["card_present", "atm"]), ts, la, lo, None, None))
            else:  # amount_spike
                m = f"M{rng.randrange(5000)}"
                la, lo = _jit(rng, p.home)
                online = rng.random() < 0.6
                eps.append(Txn(nid(), p.acct, round(typical * rng.uniform(10, 40) + 300, 2), m,
                               rng.choice(MCC_RISKY) if rng.random() < 0.4 else _mcc_for(m), CITIES[p.home][0],
                               "online" if online else "card_present", start, la, lo,
                               rng.choice(p.devices) if online and rng.random() < 0.5 else (thief_dev if online else None),
                               None))
            for e in eps:
                if e.ts < horizon:
                    out.append((e, 1, kind))

    out.sort(key=lambda r: r[0].ts)
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


CSV_COLUMNS = ["transaction_id", "account_id", "amount", "currency", "merchant_id", "merchant_category",
               "country", "channel", "timestamp", "lat", "lon", "device_id", "ip", "is_fraud"]


def write_csv(rows, path: str) -> None:
    """Write rows in the labelled-CSV format accepted by ``train --csv`` (also a template for your own export)."""
    import csv
    from datetime import datetime, timezone
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for t, label, _ in rows:
            w.writerow([t.id, t.account, t.amount, "USD", t.merchant, t.mcc, t.country, t.channel,
                        datetime.fromtimestamp(t.ts, timezone.utc).isoformat(),
                        "" if t.lat is None else round(t.lat, 5), "" if t.lon is None else round(t.lon, 5),
                        t.device or "", t.ip or "", label])
