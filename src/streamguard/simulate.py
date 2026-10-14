"""End-to-end check against a running server: ``python -m streamguard.simulate``.

Replays labelled synthetic traffic (timestamps shifted to end "now") through the
HTTP API and reports how the live system did. Requires ``httpx`` (dev extra).
"""
from __future__ import annotations

import argparse
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import httpx

from .synthetic import generate


def payload(t) -> dict:
    return {
        "transaction_id": t.id, "account_id": t.account, "amount": t.amount, "currency": "USD",
        "merchant_id": t.merchant, "merchant_category": t.mcc, "country": t.country, "channel": t.channel,
        "timestamp": datetime.fromtimestamp(t.ts, timezone.utc).isoformat(),
        "lat": t.lat, "lon": t.lon, "device_id": t.device,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--api-key", default=os.environ.get("STREAMGUARD_API_KEYS", "").split(",")[0])
    ap.add_argument("--accounts", type=int, default=300)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=2024, help="use a seed different from training")
    ap.add_argument("--fraud-share", type=float, default=0.10, help="share of accounts hit by fraud")
    a = ap.parse_args()

    rows = generate(a.accounts, a.days, a.seed, fraud_account_share=a.fraud_share)
    shift = time.time() - 120 - rows[-1][0].ts
    for t, _, _ in rows:
        t.ts += shift
    print(f"Replaying {len(rows)} transactions ({sum(r[1] for r in rows)} fraud) -> {a.url}")

    conf = Counter()
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    lat: list[float] = []
    wall = time.perf_counter()
    with httpx.Client(base_url=a.url, headers={"X-API-Key": a.api_key}, timeout=10) as c:
        for i, (t, label, kind) in enumerate(rows, 1):
            r = c.post("/v1/transactions/score", json=payload(t))
            r.raise_for_status()
            d = r.json()
            flagged = d["decision"] != "APPROVE"
            conf[(label, flagged)] += 1
            lat.append(d["latency_ms"])
            if label:
                by_type[kind][0] += 1
                by_type[kind][1] += flagged
            if i % 5000 == 0:
                print(f"  {i}/{len(rows)}")
    secs = time.perf_counter() - wall

    tp, fn, fp, tn = conf[(1, True)], conf[(1, False)], conf[(0, True)], conf[(0, False)]
    lat.sort()
    print(f"\nthroughput {len(rows)/secs:,.0f} txn/s (sequential client) | server p50 {lat[len(lat)//2]:.2f} ms  p99 {lat[int(len(lat)*.99)]:.2f} ms")
    print(f"fraud caught {tp}/{tp+fn}  recall {tp/max(tp+fn,1):.1%}")
    print(f"false positives {fp}/{fp+tn}  FPR {fp/max(fp+tn,1):.3%}  precision {tp/max(tp+fp,1):.1%}")
    for k, (n, hit) in sorted(by_type.items()):
        print(f"  {k:18s} {hit}/{n}")


if __name__ == "__main__":
    main()
