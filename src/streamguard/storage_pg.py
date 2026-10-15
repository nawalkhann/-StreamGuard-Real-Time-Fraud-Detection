"""PostgreSQL backend with the same interface as ``storage.Storage``; safe to share between instances."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .storage import DuplicateTransaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    ts             DOUBLE PRECISION NOT NULL,
    payload        JSONB NOT NULL,
    decision       TEXT NOT NULL,
    risk_score     DOUBLE PRECISION NOT NULL,
    result         JSONB NOT NULL,
    latency_ms     DOUBLE PRECISION NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_ts ON transactions(ts);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at);
CREATE TABLE IF NOT EXISTS alerts (
    id             BIGSERIAL PRIMARY KEY,
    transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(transaction_id),
    decision       TEXT NOT NULL,
    risk_score     DOUBLE PRECISION NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    analyst        TEXT,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON alerts(status, id);
CREATE TABLE IF NOT EXISTS blocklist (
    kind TEXT NOT NULL, value TEXT NOT NULL, reason TEXT, created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (kind, value)
);
"""
SCHEMA_LOCK_ID = 727_274_001  # serialises concurrent first-start schema creation across instances

ALERT_SELECT = ("SELECT a.*, t.account_id, t.payload, t.result FROM alerts a "
                "JOIN transactions t USING (transaction_id)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(r: dict) -> dict:
    payload, result = r["payload"], r["result"]
    return {
        "id": r["id"], "transaction_id": r["transaction_id"], "account_id": r["account_id"],
        "decision": r["decision"], "risk_score": r["risk_score"], "status": r["status"],
        "analyst": r["analyst"], "note": r["note"], "created_at": r["created_at"].isoformat(),
        "updated_at": r["updated_at"].isoformat(), "amount": payload["amount"], "currency": payload["currency"],
        "merchant_id": payload["merchant_id"], "country": payload["country"],
        "reasons": result.get("reasons", []),
    }


class PostgresStorage:
    def __init__(self, url: str, pool_max: int = 10):
        self._pool = ConnectionPool(url, min_size=1, max_size=pool_max, kwargs={"row_factory": dict_row},
                                    open=True, timeout=10)
        self._pool.wait(timeout=15)
        with self._pool.connection() as c:
            enc = c.execute("SHOW server_encoding").fetchone()["server_encoding"]
            enc = enc.decode() if isinstance(enc, bytes) else enc
            if enc.upper() != "UTF8":
                self._pool.close()
                raise RuntimeError(f"PostgreSQL database encoding is {enc}, but StreamGuard requires UTF8 "
                                   "(create the database with ENCODING 'UTF8'; SQL_ASCII returns raw bytes)")
            c.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
            try:
                c.execute(SCHEMA)
            finally:
                c.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))

    def ping(self) -> bool:
        with self._pool.connection() as c:
            c.execute("SELECT 1")
        return True

    def close(self) -> None:
        self._pool.close()

    # -- decisions ---------------------------------------------------------
    def get_result(self, transaction_id: str) -> dict | None:
        with self._pool.connection() as c:
            r = c.execute("SELECT result FROM transactions WHERE transaction_id=%s", (transaction_id,)).fetchone()
        return r["result"] if r else None

    def get_transaction(self, transaction_id: str) -> dict | None:
        with self._pool.connection() as c:
            r = c.execute("SELECT payload, result FROM transactions WHERE transaction_id=%s",
                          (transaction_id,)).fetchone()
        return {"transaction": r["payload"], "decision": r["result"]} if r else None

    def save_decision(self, payload: dict, ts: float, result: dict, alert: bool) -> dict | None:
        now = _now()
        with self._pool.connection() as c:  # one transaction; commits on exit, rolls back on exception
            row = c.execute(
                "INSERT INTO transactions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (transaction_id) DO NOTHING RETURNING transaction_id",
                (payload["transaction_id"], payload["account_id"], ts, Jsonb(payload), result["decision"],
                 result["risk_score"], Jsonb(result), result["latency_ms"], now)).fetchone()
            if row is None:
                raise DuplicateTransaction(payload["transaction_id"])
            if not alert:
                return None
            a = c.execute(
                "INSERT INTO alerts (transaction_id, decision, risk_score, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (payload["transaction_id"], result["decision"], result["risk_score"], now, now)).fetchone()
            return _fmt(c.execute(ALERT_SELECT + " WHERE a.id=%s", (a["id"],)).fetchone())

    def history_payloads(self, since_ts: float):
        with self._pool.connection() as c:
            rows = c.execute("SELECT payload FROM transactions WHERE ts >= %s ORDER BY ts", (since_ts,)).fetchall()
        for r in rows:
            yield r["payload"]

    def account_history(self, account_id: str, since_ts: float):
        with self._pool.connection() as c:
            rows = c.execute("SELECT payload FROM transactions WHERE account_id=%s AND ts >= %s ORDER BY ts",
                             (account_id, since_ts)).fetchall()
        for r in rows:
            yield r["payload"]

    def labelled_history(self):
        with self._pool.connection() as c:
            rows = c.execute("SELECT t.payload, t.ts, t.decision, a.status FROM transactions t "
                             "LEFT JOIN alerts a USING (transaction_id) ORDER BY t.ts").fetchall()
        for r in rows:
            yield r["payload"], r["ts"], r["status"], r["decision"]

    def max_ts(self) -> float | None:
        with self._pool.connection() as c:
            return c.execute("SELECT MAX(ts) AS m FROM transactions").fetchone()["m"]

    # -- alerts ------------------------------------------------------------
    def list_alerts(self, status: str | None, limit: int, offset: int) -> list[dict]:
        q, args = ALERT_SELECT, []
        if status:
            q += " WHERE a.status=%s"
            args.append(status)
        q += " ORDER BY a.id DESC LIMIT %s OFFSET %s"
        args += [limit, offset]
        with self._pool.connection() as c:
            return [_fmt(r) for r in c.execute(q, args).fetchall()]

    def update_alert(self, alert_id: int, status: str, analyst: str, note: str | None) -> dict | None:
        with self._pool.connection() as c:
            n = c.execute("UPDATE alerts SET status=%s, analyst=%s, note=%s, updated_at=%s WHERE id=%s",
                          (status, analyst, note, _now(), alert_id)).rowcount
            if not n:
                return None
            return _fmt(c.execute(ALERT_SELECT + " WHERE a.id=%s", (alert_id,)).fetchone())

    # -- blocklist ---------------------------------------------------------
    def blocklist(self) -> list[dict]:
        with self._pool.connection() as c:
            rows = c.execute("SELECT * FROM blocklist ORDER BY created_at DESC").fetchall()
        return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]

    def block(self, kind: str, value: str, reason: str | None) -> None:
        with self._pool.connection() as c:
            c.execute("INSERT INTO blocklist VALUES (%s,%s,%s,%s) ON CONFLICT (kind, value) "
                      "DO UPDATE SET reason=EXCLUDED.reason, created_at=EXCLUDED.created_at",
                      (kind, value, reason, _now()))

    def unblock(self, kind: str, value: str) -> bool:
        with self._pool.connection() as c:
            return c.execute("DELETE FROM blocklist WHERE kind=%s AND value=%s", (kind, value)).rowcount > 0

    # -- stats -------------------------------------------------------------
    def stats(self, since_iso: str) -> dict:
        since = datetime.fromisoformat(since_iso)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        with self._pool.connection() as c:
            by_dec = c.execute("SELECT decision, COUNT(*) AS c FROM transactions WHERE created_at>=%s GROUP BY decision",
                               (since,)).fetchall()
            lat = c.execute("SELECT percentile_disc(ARRAY[0.5,0.95,0.99]) WITHIN GROUP (ORDER BY latency_ms) AS p "
                            "FROM transactions WHERE created_at>=%s", (since,)).fetchone()["p"]
            alerts = c.execute("SELECT status, COUNT(*) AS c FROM alerts WHERE created_at>=%s GROUP BY status",
                               (since,)).fetchall()
            open_total = c.execute("SELECT COUNT(*) AS c FROM alerts WHERE status='open'").fetchone()["c"]
        decisions = {r["decision"]: r["c"] for r in by_dec}
        p = (lambda i: round(lat[i], 2)) if lat and lat[0] is not None else (lambda i: None)
        return {"total": sum(decisions.values()), "decisions": decisions,
                "alerts_by_status": {r["status"]: r["c"] for r in alerts}, "open_alerts_total": open_total,
                "latency_ms": {"p50": p(0), "p95": p(1), "p99": p(2)}}
