"""Concurrent load + correctness test across one or more StreamGuard instances.

  python scripts/loadtest.py --api-key KEY --url http://h1:8000 --url http://h2:8000 --workers 32

Replays labelled synthetic traffic (timestamps shifted to end now). Each account's transactions are sent in order
by a single worker, requests round-robin across the given URLs, so you exercise cross-instance consistency.
Reports throughput, client-side latency percentiles, errors, and detection quality (recall / false-positive rate).
"""
import argparse
import asyncio
import itertools
import time
import zlib
from collections import Counter
from datetime import datetime, timezone

import httpx

from streamguard.synthetic import generate


def body(t) -> dict:
    return {"transaction_id": t.id, "account_id": t.account, "amount": t.amount, "currency": "USD",
            "merchant_id": t.merchant, "merchant_category": t.mcc, "country": t.country, "channel": t.channel,
            "timestamp": datetime.fromtimestamp(t.ts, timezone.utc).isoformat(),
            "lat": t.lat, "lon": t.lon, "device_id": t.device}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--accounts", type=int, default=400)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--seed", type=int, default=909)
    a = ap.parse_args()

    rows = generate(a.accounts, a.days, a.seed, fraud_account_share=0.10)
    shift = time.time() - 120 - rows[-1][0].ts
    lanes = [[] for _ in range(a.workers)]
    for t, label, kind in rows:                      # rows are time-sorted; one lane per account keeps order
        t.ts += shift
        lanes[zlib.crc32(t.account.encode()) % a.workers].append((t, label, kind))

    rr = itertools.cycle(range(len(a.url)))
    lat, conf, errors, per_url = [], Counter(), Counter(), Counter()
    clients = [httpx.AsyncClient(base_url=u, headers={"X-API-Key": a.api_key}, timeout=30,
                                 limits=httpx.Limits(max_connections=a.workers)) for u in a.url]

    async def run(lane):
        for t, label, _ in lane:
            i = next(rr)
            t0 = time.perf_counter()
            try:
                r = await clients[i].post("/v1/transactions/score", json=body(t))
                r.raise_for_status()
            except Exception as e:
                errors[type(e).__name__] += 1
                continue
            lat.append((time.perf_counter() - t0) * 1000)
            per_url[a.url[i]] += 1
            conf[(label, r.json()["decision"] != "APPROVE")] += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(run(l) for l in lanes))
    wall = time.perf_counter() - t0
    await asyncio.gather(*(c.aclose() for c in clients))

    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))]  # noqa: E731
    tp, fn, fp, tn = conf[(1, True)], conf[(1, False)], conf[(0, True)], conf[(0, False)]
    print(f"instances={len(a.url)} workers={a.workers} txns={len(lat)} errors={dict(errors) or 0} wall={wall:.1f}s")
    print(f"throughput {len(lat)/wall:,.0f} txn/s | client latency ms p50 {q(.5):.1f}  p95 {q(.95):.1f}  p99 {q(.99):.1f}")
    print(f"requests per instance: {dict(per_url)}")
    print(f"fraud caught {tp}/{tp+fn} ({tp/max(tp+fn,1):.1%}) | false positives {fp}/{fp+tn} ({fp/max(fp+tn,1):.3%})")


if __name__ == "__main__":
    asyncio.run(main())
