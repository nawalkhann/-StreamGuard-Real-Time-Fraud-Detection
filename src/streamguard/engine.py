"""Decision engine: features -> rules + ML -> APPROVE / REVIEW / DECLINE with reason codes."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from . import rules
from .config import Settings
from .features import FeatureStore
from .metrics import Metrics
from .model import FraudModel
from .schemas import Decision, Reason, Transaction
from .storage import DuplicateTransaction, Storage

log = logging.getLogger(__name__)

FEATURE_LABELS = {
    "amount_zscore": "Unusual amount for this account",
    "log_amount": "Large amount",
    "n_1m": "Burst of transactions in the last minute",
    "n_10m": "High transaction velocity (10 min)",
    "n_1h": "High transaction velocity (1 h)",
    "n_small_10m": "Several micro-transactions (card testing pattern)",
    "new_device": "Never-seen device",
    "new_merchant": "First time at this merchant",
    "country_mismatch": "Transaction outside the account's usual country",
    "log_dist_km": "Large distance from previous transaction",
    "log_speed_kmh": "Implausible travel speed",
    "high_risk_mcc": "High-risk merchant category",
    "is_online": "Card-not-present channel",
    "distinct_merchants_1h": "Many distinct merchants in an hour",
    "log_secs_since_last": "Very short time since last transaction",
}


class SchemaError(ValueError):
    """Caller supplied something we cannot score (e.g. unsupported currency)."""


class Engine:
    def __init__(self, settings: Settings, storage: Storage, model: FraudModel | None):
        self.s = settings
        self.db = storage
        self.model = model
        self.metrics = Metrics()
        self.alert_listeners: list = []  # callables(alert_dict), invoked from the scoring thread
        self.store = FeatureStore(settings.high_risk_mcc_set, settings.max_accounts_in_memory)
        self._blocked: frozenset = frozenset()
        self.reload_blocklist()
        if model is None:
            log.warning("No ML model loaded: running in rules-only mode. Run `python -m streamguard.train`.")

    # -- lifecycle ---------------------------------------------------------
    def reload_blocklist(self) -> None:
        self._blocked = frozenset((b["kind"], b["value"]) for b in self.db.blocklist())

    def warm_start(self) -> int:
        """Rebuild in-memory feature state from the persisted transaction log."""
        latest = self.db.max_ts()
        if latest is None:
            return 0
        n = 0
        for payload in self.db.history_payloads(latest - self.s.warm_start_days * 86_400):
            try:
                t = Transaction.model_validate(payload).to_internal(self.s.fx_rates)
            except (ValidationError, ValueError):
                continue
            self.store.commit(t)
            n += 1
        log.info("Warm start: replayed %d transactions into %d accounts", n, len(self.store))
        return n

    # -- scoring -----------------------------------------------------------
    def score(self, txn: Transaction) -> Decision:
        t0 = time.perf_counter()
        try:
            t = txn.to_internal(self.s.fx_rates)
        except ValueError as e:
            raise SchemaError(str(e)) from e
        if t.ts > time.time() + self.s.max_future_skew_seconds:
            raise SchemaError("timestamp is too far in the future")

        with self.store.account_lock(t.account):
            prior = self.db.get_result(txn.transaction_id)
            if prior is not None:  # idempotent replay
                self.metrics.duplicate()
                prior["duplicate"] = True
                return Decision.model_validate(prior)

            feats = self.store.compute(t)
            hits = rules.evaluate(t, feats, self._blocked, self.s.review_amount_usd)
            rule_p = rules.combine(hits)
            reasons = [Reason(code=h.code, description=h.description, weight=round(h.weight, 3)) for h in hits]

            ml_p: float | None = None
            if self.model is not None:
                ml_p = self.model.predict(feats)
                if ml_p >= self.s.review_threshold * 0.6:
                    for name, delta in self.model.explain(feats):
                        reasons.append(Reason(
                            code=f"ML_{name.upper()}", description=FEATURE_LABELS.get(name, name),
                            weight=round(delta, 3)))

            combined = rule_p if ml_p is None else 1.0 - (1.0 - rule_p) * (1.0 - ml_p)
            if any(h.hard for h in hits) or combined >= self.s.decline_threshold:
                outcome = "DECLINE"
            elif combined >= self.s.review_threshold:
                outcome = "REVIEW"
            else:
                outcome = "APPROVE"

            latency_ms = (time.perf_counter() - t0) * 1000
            decision = Decision(
                transaction_id=txn.transaction_id, decision=outcome, risk_score=round(combined * 100, 2),
                ml_score=None if ml_p is None else round(ml_p, 4), rule_score=round(rule_p, 4),
                reasons=reasons, model_version=self.model.version if self.model else None,
                latency_ms=round(latency_ms, 3), scored_at=datetime.now(timezone.utc),
            )
            try:
                alert = self.db.save_decision(
                    txn.model_dump(mode="json"), t.ts, decision.model_dump(mode="json"),
                    alert=outcome != "APPROVE")
            except DuplicateTransaction:
                # same id raced in on a different account lock; return the stored decision
                prior = self.db.get_result(txn.transaction_id)
                self.metrics.duplicate()
                prior["duplicate"] = True
                return Decision.model_validate(prior)
            self.store.commit(t)

        self.metrics.observe(outcome, time.perf_counter() - t0)
        if alert is not None:
            for fn in self.alert_listeners:
                try:
                    fn(alert)
                except Exception:  # a broken subscriber must never break scoring
                    log.exception("alert listener failed")
        return decision
