# StreamGuard — Real-Time Fraud Detection

A self-contained service that scores payment transactions in real time and returns
**APPROVE / REVIEW / DECLINE** with human-readable reason codes. Flagged transactions land in an
analyst queue with a live dashboard and an audit trail.

```
 transactions ──► HTTP API  ┐                       ┌─► APPROVE
 (or Kafka topic)           ├─► streaming features ─┼─► REVIEW  ──► analyst queue (live dashboard)
                            ┘   + rules + ML model  └─► DECLINE        │ confirm fraud / false positive
                                     │                                 ▼
                                     └──── SQLite audit log ◄── dispositions (future training labels)
```

## Quick start

```bash
make install                      # venv + dependencies
make train                        # bootstrap model on synthetic data (~10 s)
cp .env.example .env              # set STREAMGUARD_API_KEYS
make run                          # http://127.0.0.1:8000  (dashboard at /, API docs at /docs)
make simulate                     # in another shell: replay labelled traffic, print detection report
make test
```

Score a transaction:

```bash
curl -X POST localhost:8000/v1/transactions/score -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{
  "transaction_id": "tx-1001", "account_id": "acct-77", "amount": 249.99, "currency": "USD",
  "merchant_id": "m-5521", "merchant_category": "5732", "country": "US", "channel": "online",
  "timestamp": "2026-10-14T10:15:00Z", "lat": 40.71, "lon": -74.0, "device_id": "dev-abc"}'
```

```json
{"transaction_id":"tx-1001","decision":"APPROVE","risk_score":3.1,"ml_score":0.031,"rule_score":0.0,
 "reasons":[],"model_version":"gbm-96320d90","latency_ms":1.2,"duplicate":false, "...": "..."}
```

## See it work

```bash
make run                                     # terminal 1 (set STREAMGUARD_API_KEYS first)
python scripts/demo.py --api-key $KEY        # terminal 2: narrated attack scenarios, one decision per line
make simulate                                # bulk labelled traffic + detection report
```
Open http://127.0.0.1:8000, enter the key and your name, and watch alerts arrive live. `scripts/demo.py` covers: normal
purchase, card testing, account takeover, impossible travel, sanctions hits (fictitious names), structuring +
high-risk jurisdiction, idempotent retry, and blocklisting.

## How a decision is made

1. **Streaming features** (`features.py`) — per-account sliding windows computed from event time:
   1m/10m/1h/24h velocity, micro-transaction count (card testing), amount z-score vs the account's own
   history, new device / merchant / country, distance and implied speed from the previous geolocated
   transaction, hour of day, channel, merchant-category risk. The *same code* builds training data, so
   there is no train/serve skew.
2. **Rules** (`rules.py`) — transparent, weighted, combined by noisy-OR. Blocklist hits are hard declines.
3. **ML** (`model.py`, `train.py`) — gradient-boosted trees. The top contributing features are returned as
   reason codes (`ML_*`) via occlusion attribution.
4. **Combine** — `risk = 1 − (1−rule)(1−ml)`; `≥ REVIEW_THRESHOLD` → review, `≥ DECLINE_THRESHOLD` → decline.

Operational properties: idempotent on `transaction_id` (safe for retries / at-least-once delivery),
tolerant of out-of-order events, bounded memory (LRU), feature state rebuilt from the audit log on restart,
API-key auth with constant-time comparison, extra fields rejected (so raw card data can't sneak in),
Prometheus metrics at `/metrics`, `/healthz` and `/readyz` probes.

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/transactions/score` | Score one transaction (idempotent) |
| `GET /v1/transactions/{id}` | Stored transaction + decision (audit) |
| `GET /v1/alerts?status=` / `PATCH /v1/alerts/{id}` | Analyst queue and disposition |
| `GET/POST/DELETE /v1/blocklist` | Account / device / IP / merchant blocklist (effective immediately) |
| `GET /v1/stats?hours=` | Volumes, decision mix, latency percentiles |
| `WS /ws/alerts?api_key=` | Live alert stream |
| `/healthz` `/readyz` `/metrics` | Ops (unauthenticated — keep internal) |

Optional Kafka ingestion: `pip install -e '.[kafka]'` and set `STREAMGUARD_KAFKA_BOOTSTRAP`.
Messages from `transactions` are scored and published to `fraud-decisions`; malformed ones go to
`transactions-dlq`. Key/partition the input topic by `account_id`.

## Measured results — read before trusting

All numbers come from **synthetic data** produced by `synthetic.py`, which models four fraud patterns
(card testing, account takeover, stolen card / impossible travel, amount spike) plus legitimate noise
(travel, big purchases, new devices).

* In-pipeline chronological hold-out: ROC-AUC ≈ 1.0 — **this is an artefact of cleanly separable synthetic fraud, not a
  prediction of real-world performance.**
* End-to-end via the live API on unseen traffic (`make simulate`, 8,092 txns, 142 fraud): **87% of fraud flagged,
  0.35% false-positive rate, 82% precision**; server-side latency p50 ≈ 1 ms, p99 ≈ 6 ms. Weakest patterns:
  stolen-card (14/25) and one-off amount spikes (6/11), which genuinely resemble legitimate behaviour.

## Counter-terrorism financing & sanctions screening

There is **no public labelled dataset of terrorism-financing transactions** (real cases are rare and confidential), so
this is done the way compliance teams do it: public designation lists, risk-based jurisdiction rules and published
typologies, with analysts adjudicating hits. No ML model is claimed for this.

| Control | Behaviour |
|---|---|
| **Sanctions name screening** of `counterparty_name` | Exact normalised match to a list entry/alias → `DECLINE` (hard). Fuzzy match (word order, spelling variants, accents; >= 2 words, similarity >= 0.90) → `REVIEW`. Reason text names the list and entry id. |
| **High-risk jurisdiction** | Counterparty or transaction country in `STREAMGUARD_HIGH_RISK_JURISDICTIONS` (default `KP,IR,MM`, **verify against the current FATF publication**) → risk +0.50. Based on where money goes, never the customer's nationality. |
| **Structuring** | 3+ amounts in 80–100% of the reporting threshold (default 10,000 USD) within 24 h → risk +0.55. |

Lists are files you place in `STREAMGUARD_SANCTIONS_DIR` (default `data/sanctions/`):

| File | Source |
|---|---|
| `sdn.csv` + `alt.csv` | OFAC SDN list and aliases (https://ofac.treasury.gov/sanctions-list-service) |
| `consolidated.xml` | UN Security Council consolidated list |
| `custom*.csv` | Any other list (EU, your internal watchlist): header `name,id[,list]` |

`scripts/fetch_sanctions.sh` downloads the first two. `/readyz` reports names loaded per list and warns if a list is
older than 7 days or none is loaded. Restart the service to reload.

**Limits you must know**
* **Verified on fictitious fixtures only.** The OFAC/UN loaders were written to the published file layouts but have not
  yet been run on the real downloads (the sandbox's network policy blocked those hosts). Run them once on the real files
  and check the loaded counts in `/readyz` before relying on them.
* Hits are **candidates for human review, not determinations**. Names produce false positives; confirm against the
  source list and follow your institution's legal blocking/reporting process (e.g. SAR/STR filing is out of scope here).
* Lists change daily; stale lists are a compliance failure. Automate the fetch.
* Only the counterparty name is screened, and only if the caller sends it. Screening your own customers (KYC) and
  other lists (EU, UK, local) are separate work. Screening latency is ~2.4 ms on a 40k-name list.
* Latin-script matching only; no transliteration of Arabic/Cyrillic/CJK names.

## Scaling out (multiple instances)

Default mode is one process: SQLite + in-memory feature state. Set two variables to run any number of identical
instances behind a plain load balancer:

| Variable | Effect |
|---|---|
| `STREAMGUARD_DATABASE_URL=postgresql://user:pass@host/db` | Shared audit log, alerts, blocklist (database must be UTF8; checked at startup) |
| `STREAMGUARD_REDIS_URL=redis://host:6379/0` | Shared per-account feature state; alert fan-out to every instance's dashboard; blocklist invalidation. **Requires the database URL.** |

```bash
STREAMGUARD_API_KEYS=... docker compose -f docker-compose.scale.yml up --build   # 2 instances + Postgres + Redis + nginx on :8000
```

How it stays correct
* Each transaction takes a short **per-account Redis lock**; state is loaded, features computed, the decision saved to
  Postgres, then state is written back **fenced** (refused if the lock lease expired, which invalidates the cached state).
* **Postgres is the source of truth.** If Redis is flushed or an account expires, its state is rebuilt from stored
  transactions on next use. Scoring is idempotent on `transaction_id` across instances.
* **Redis down = scoring fails (HTTP 500), never silently scores with empty history.** Decide at your gateway whether to
  fail open or closed; run Redis with persistence/replication for HA (not configured here).
* Blocklist changes reach other instances within milliseconds (pub/sub) and at worst within
  `STREAMGUARD_BLOCKLIST_REFRESH_SECONDS` (default 30).
* Trade-off: ~3 extra Redis round trips per transaction versus in-memory state. Measure on your hardware
  (`scripts/loadtest.py --url http://a --url http://b`); there are no published scale numbers for this mode yet.

Verified: contract tests run the same suite against SQLite and Postgres; distributed tests (real Postgres + Redis)
cover identical features vs. the memory store, recovery after a Redis flush, fencing, exactly-once under 80 concurrent
deliveries across two instances, cross-instance alerts/blocklist, and Redis outage. Deliberately breaking the lock makes
the exactly-once test fail (37/40 updates survived). CI runs all of it plus a compose smoke test. **Not verified:**
throughput scaling, Redis/Postgres failover behaviour, and the compose stack outside CI.

## Training on your real labelled data

```bash
# 1. See the expected format (also a template for your own export)
python -m streamguard.train --write-sample sample.csv

# 2a. Train from a labelled history export (CSV, one row per transaction, is_fraud = 0/1)
python -m streamguard.train --csv my_transactions.csv

# 2b. ...or from analyst dispositions collected by this service, optionally with chargebacks
python -m streamguard.train --from-db --labels chargebacks.csv     # chargebacks.csv: transaction_id,is_fraud
```

* All rows (labelled or not) are replayed in time order so account history is correct; the model is evaluated on a
  **chronological 25% hold-out**, never a random split.
* It **refuses to train** with fewer than 200 fraud examples in the training window or 20 in the hold-out
  (`--min-positives` to override knowingly).
* It prints a precision/recall table per threshold and suggests `REVIEW`/`DECLINE` thresholds meeting precision
  targets (`--review-precision`, `--decline-precision`). Treat as a starting point; apply your own cost model.
* **Label caveats:** with `--from-db`, un-alerted approvals are assumed legitimate, so undetected fraud pollutes the
  negatives; supply chargebacks via `--labels` (they win over dispositions). Alerts still `open` are excluded.
  Labels are also biased by what the current system chose to flag. Prefer a history export that pre-dates StreamGuard.
* Restart the service after training; check `/readyz` for the new `model` version.

## Before using this on real money

* **Retrain on your own labelled history.** The bundled model is a bootstrap so the system runs on day one.
  Retrain with `--csv` / `--from-db` (see above). Re-tune thresholds on your data and cost of fraud vs.
  customer friction.
* **Never send PANs.** `account_id` must be a token. The service stores transaction payloads in SQLite; apply your
  PCI/GDPR retention and encryption-at-rest policy (volume encryption, retention job).
* **Single-process by design.** Feature state is in memory, so run one worker per instance and shard by account
  upstream. SQLite is fine for tens/hundreds of txn/s; move to Postgres + Redis (or Flink) for more or for HA.
  The simulator's sequential client reached ~280 txn/s; a concurrency test covers duplicate deliveries, but there
  has been no load test beyond that.
* **Model artefacts are pickles** (joblib) — only load files you produced. 
* **FX rates are a static table** (`config.py`); replace with a rates feed.
* **Verified in CI** (GitHub Actions): tests on Python 3.11 / 3.12 / 3.13, and the Docker image builds, starts, serves
  the dashboard assets and scores a transaction.
* **Not yet verified:** the Kafka adapter against a real broker (the transport-independent `process_message` core is
  unit-tested), and the OFAC/UN list loaders on real downloads (see the sanctions section).
* Single shared API-key scheme and free-text analyst names; add SSO/RBAC before multi-team use.
  Expose `/metrics` and `/readyz` only on an internal network.

## Layout

```
src/streamguard/  config · schemas · features · rules · screening · model · engine · storage · metrics · api · ingest
                  synthetic (data gen) · labelled (CSV/DB loaders) · train · simulate · static/ (dashboard)
tests/            features, rules, API (auth, idempotency, workflow, websocket, restart, concurrency), model, ingest
```
