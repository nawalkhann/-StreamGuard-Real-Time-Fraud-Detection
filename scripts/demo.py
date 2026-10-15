"""Narrated walk-through against a running server: ``python scripts/demo.py --api-key KEY``.

Builds normal histories for a few accounts, then replays classic attack patterns and prints each decision.
Names used for sanctions hits are FICTITIOUS test entries, not real designations.
"""
import argparse
import time
import uuid
from datetime import datetime, timezone

import httpx

NOW = time.time()
RUN = uuid.uuid4().hex[:6]


def iso(offset_s: float) -> str:
    return datetime.fromtimestamp(NOW + offset_s, timezone.utc).isoformat()


class Demo:
    def __init__(self, c: httpx.Client):
        self.c, self.n = c, 0

    def tx(self, acct, amount, off, **kw):
        self.n += 1
        body = {"transaction_id": f"demo-{RUN}-{self.n}", "account_id": f"{acct}-{RUN}", "amount": amount,
                "currency": "USD", "merchant_id": kw.pop("merchant", "M-grocer"), "merchant_category": kw.pop("mcc", "5411"),
                "country": kw.pop("country", "US"), "channel": kw.pop("channel", "card_present"),
                "timestamp": iso(off), "lat": kw.pop("lat", 40.71), "lon": kw.pop("lon", -74.0), **kw}
        r = self.c.post("/v1/transactions/score", json=body)
        r.raise_for_status()
        return body, r.json()

    def history(self, acct, n=14):
        for i in range(n):
            self.tx(acct, 18 + (i * 7) % 25, -86400 * 3 + i * 3600 * 4, device_id="phone-1",
                    merchant=f"M-fav{i % 4}", channel="online" if i % 3 == 0 else "card_present")

    def show(self, title, rows):
        print(f"\n=== {title}")
        for body, d in rows:
            why = "; ".join(r["description"] for r in d["reasons"][:3]) or "-"
            ml = "n/a" if d["ml_score"] is None else f"{d['ml_score']:.2f}"
            print(f"  {d['decision']:8s} risk {d['risk_score']:5.1f}  ml {ml}  ${body['amount']:>9,.2f} {body['country']} "
                  f"{body['channel']:<12s} | {why}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--api-key", required=True)
    a = ap.parse_args()
    with httpx.Client(base_url=a.url, headers={"X-API-Key": a.api_key}, timeout=10) as c:
        d = Demo(c)
        print("Ready:", c.get("/readyz").json())
        for acct in ("alice", "bob", "carol", "dave", "erin"):
            d.history(acct)

        d.show("1. Normal everyday purchase", [d.tx("alice", 22.5, -300, device_id="phone-1", merchant="M-fav1")])

        d.show("2. Card testing: bot tries many tiny charges on a stolen card, then cashes out",
               [d.tx("bob", 1.0 + i * 0.3, -240 + i * 12, channel="online", device_id="bot-9", merchant=f"M-shop{i}")
                for i in range(6)] + [d.tx("bob", 950, -60, channel="online", device_id="bot-9", merchant="M-elec", mcc="5732")])

        d.show("3. Account takeover: new device, foreign IP, high-risk merchant category, large amounts",
               [d.tx("carol", 1800, -2400, channel="online", device_id="unknown-77", country="NG", lat=6.5, lon=3.4,
                     merchant="M-wire", mcc="4829"),
                d.tx("carol", 2400, -1200, channel="online", device_id="unknown-77", country="NG", lat=6.5, lon=3.4,
                     merchant="M-bet", mcc="7995")])

        d.show("4. Impossible travel: New York purchase, then London in-store 40 minutes later",
               [d.tx("dave", 35, -5000, merchant="M-fav0"),
                d.tx("dave", 410, -2600, country="GB", lat=51.5, lon=-0.13, merchant="M-london")])

        d.show("5. Sanctions screening (FICTITIOUS list entries): exact name, reordered/misspelt name, clean name",
               [d.tx("erin", 600, -900, channel="online", device_id="phone-1", counterparty_name="Northwind Relief Foundation"),
                d.tx("erin", 450, -800, channel="online", device_id="phone-1", counterparty_name="Zarkov Ivan Petrovitch"),
                d.tx("erin", 450, -700, channel="online", device_id="phone-1", counterparty_name="Acme Hardware Supplies")])

        d.show("6. Structuring + high-risk jurisdiction: repeated transfers just under the 10,000 reporting threshold",
               [d.tx("erin", 9400 + i * 100, -600 + i * 120, channel="online", device_id="phone-1", counterparty_country="IR")
                for i in range(4)])

        body, first = d.tx("alice", 22.5, -100, device_id="phone-1")
        r = c.post("/v1/transactions/score", json=body)
        print(f"\n=== 7. Retry of the same transaction_id (at-least-once delivery): "
              f"duplicate={r.json()['duplicate']}, Idempotent-Replay header={r.headers.get('Idempotent-Replay')}, same decision={r.json()['decision']==first['decision']}")

        c.post("/v1/blocklist", json={"kind": "device", "value": "stolen-phone-5", "reason": "reported stolen"})
        d.show("8. Blocklisted device (analyst added it seconds ago; effective immediately)",
               [d.tx("alice", 12, -50, device_id="stolen-phone-5", channel="online")])


if __name__ == "__main__":
    main()
