"""SQLite persistence: decisions (audit log), analyst alerts, blocklist."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL,
    ts             REAL NOT NULL,
    payload        TEXT NOT NULL,
    decision       TEXT NOT NULL,
    risk_score     REAL NOT NULL,
    result         TEXT NOT NULL,
    latency_ms     REAL NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tx_ts ON transactions(ts);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(transaction_id),
    decision       TEXT NOT NULL,
    risk_score     REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    analyst        TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_status ON alerts(status, id);

CREATE TABLE IF NOT EXISTS blocklist (
    kind TEXT NOT NULL, value TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
"""


class DuplicateTransaction(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    def ping(self) -> bool:
        with self._lock:
            self._db.execute("SELECT 1").fetchone()
        return True

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- decisions ---------------------------------------------------------
    def get_result(self, transaction_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT result FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
        return json.loads(r["result"]) if r else None

    def get_transaction(self, transaction_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute(
                "SELECT payload, result FROM transactions WHERE transaction_id=?", (transaction_id,)
            ).fetchone()
        return {"transaction": json.loads(r["payload"]), "decision": json.loads(r["result"])} if r else None

    def save_decision(self, payload: dict, ts: float, result: dict, alert: bool) -> dict | None:
        """Persist atomically. Returns the alert row if one was raised."""
        now = _now()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
                    (payload["transaction_id"], payload["account_id"], ts, json.dumps(payload),
                     result["decision"], result["risk_score"], json.dumps(result),
                     result["latency_ms"], now),
                )
                row = None
                if alert:
                    cur = self._db.execute(
                        "INSERT INTO alerts (transaction_id, decision, risk_score, created_at, updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (payload["transaction_id"], result["decision"], result["risk_score"], now, now),
                    )
                    row = self._alert_row(cur.lastrowid)
                self._db.execute("COMMIT")
                return row
            except sqlite3.IntegrityError as e:
                self._db.execute("ROLLBACK")
                raise DuplicateTransaction(payload["transaction_id"]) from e
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def history_payloads(self, since_ts: float):
        """Yield stored payloads (ordered by event time) for state rebuild."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM transactions WHERE ts >= ? ORDER BY ts", (since_ts,)
            ).fetchall()
        for r in rows:
            yield json.loads(r["payload"])

    def max_ts(self) -> float | None:
        with self._lock:
            r = self._db.execute("SELECT MAX(ts) AS m FROM transactions").fetchone()
        return r["m"]

    # -- alerts ------------------------------------------------------------
    def _alert_row(self, alert_id: int) -> dict:
        r = self._db.execute(
            "SELECT a.*, t.account_id, t.payload, t.result FROM alerts a "
            "JOIN transactions t USING (transaction_id) WHERE a.id=?", (alert_id,)
        ).fetchone()
        return self._format_alert(r)

    @staticmethod
    def _format_alert(r: sqlite3.Row) -> dict:
        payload, result = json.loads(r["payload"]), json.loads(r["result"])
        return {
            "id": r["id"], "transaction_id": r["transaction_id"], "account_id": r["account_id"],
            "decision": r["decision"], "risk_score": r["risk_score"], "status": r["status"],
            "analyst": r["analyst"], "note": r["note"], "created_at": r["created_at"],
            "updated_at": r["updated_at"], "amount": payload["amount"], "currency": payload["currency"],
            "merchant_id": payload["merchant_id"], "country": payload["country"],
            "reasons": result.get("reasons", []),
        }

    def list_alerts(self, status: str | None, limit: int, offset: int) -> list[dict]:
        q = ("SELECT a.*, t.account_id, t.payload, t.result FROM alerts a "
             "JOIN transactions t USING (transaction_id)")
        args: list = []
        if status:
            q += " WHERE a.status=?"
            args.append(status)
        q += " ORDER BY a.id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        return [self._format_alert(r) for r in rows]

    def update_alert(self, alert_id: int, status: str, analyst: str, note: str | None) -> dict | None:
        with self._lock:
            cur = self._db.execute(
                "UPDATE alerts SET status=?, analyst=?, note=?, updated_at=? WHERE id=?",
                (status, analyst, note, _now(), alert_id),
            )
            return self._alert_row(alert_id) if cur.rowcount else None

    # -- blocklist ---------------------------------------------------------
    def blocklist(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM blocklist ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def block(self, kind: str, value: str, reason: str | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO blocklist VALUES (?,?,?,?)", (kind, value, reason, _now())
            )

    def unblock(self, kind: str, value: str) -> bool:
        with self._lock:
            return self._db.execute("DELETE FROM blocklist WHERE kind=? AND value=?", (kind, value)).rowcount > 0

    # -- stats -------------------------------------------------------------
    def stats(self, since_iso: str) -> dict:
        with self._lock:
            by_dec = self._db.execute(
                "SELECT decision, COUNT(*) c FROM transactions WHERE created_at>=? GROUP BY decision", (since_iso,)
            ).fetchall()
            lat = [r[0] for r in self._db.execute(
                "SELECT latency_ms FROM transactions WHERE created_at>=? ORDER BY latency_ms", (since_iso,))]
            alerts = self._db.execute(
                "SELECT status, COUNT(*) c FROM alerts WHERE created_at>=? GROUP BY status", (since_iso,)
            ).fetchall()
            open_total = self._db.execute("SELECT COUNT(*) FROM alerts WHERE status='open'").fetchone()[0]
        decisions = {r["decision"]: r["c"] for r in by_dec}
        total = sum(decisions.values())
        p = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 2) if lat else None  # noqa: E731
        return {
            "total": total, "decisions": decisions,
            "alerts_by_status": {r["status"]: r["c"] for r in alerts},
            "open_alerts_total": open_total,
            "latency_ms": {"p50": p(0.50), "p95": p(0.95), "p99": p(0.99)},
        }
