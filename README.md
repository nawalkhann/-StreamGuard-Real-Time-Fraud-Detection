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
* **Not yet verified:** the Docker image (no Docker daemon was available when this was written) and the Kafka
  adapter against a real broker (the transport-independent `process_message` core is unit-tested).
* Single shared API-key scheme and free-text analyst names; add SSO/RBAC before multi-team use.
  Expose `/metrics` and `/readyz` only on an internal network.

## Layout

```
src/streamguard/  config · schemas · features · rules · model · engine · storage · metrics · api · ingest
                  synthetic (data gen) · labelled (CSV/DB loaders) · train · simulate · static/ (dashboard)
tests/            features, rules, API (auth, idempotency, workflow, websocket, restart, concurrency), model, ingest
```
