"""Transparent, auditable rules. Each hit carries a weight in (0,1]; hard hits force DECLINE."""
from __future__ import annotations

from dataclasses import dataclass

from .features import Txn


@dataclass(frozen=True, slots=True)
class RuleHit:
    code: str
    description: str
    weight: float
    hard: bool = False


Blocklist = frozenset  # frozenset[tuple[kind, value]]


def evaluate(t: Txn, f: dict[str, float], blocked: frozenset, review_amount_usd: float,
             high_risk_countries: frozenset = frozenset()) -> list[RuleHit]:
    hits: list[RuleHit] = []

    for kind, val in (("account", t.account), ("device", t.device), ("ip", t.ip), ("merchant", t.merchant)):
        if val is not None and (kind, val) in blocked:
            hits.append(RuleHit("BLOCKLIST", f"{kind} is on the blocklist", 1.0, hard=True))

    # Physically implausible movement between two geolocated transactions.
    if f["log_speed_kmh"] > 6.8 and f["log_dist_km"] > 5.7:  # >~900 km/h and >~300 km
        hits.append(RuleHit("IMPOSSIBLE_TRAVEL", "Location change faster than commercial flight", 0.60))

    if f["n_small_10m"] >= 3:
        hits.append(RuleHit("CARD_TESTING", "Multiple micro-transactions in 10 minutes", 0.55))

    if f["n_1m"] >= 4:
        hits.append(RuleHit("VELOCITY_1M", "4+ prior transactions in the last minute", 0.45))
    elif f["n_10m"] >= 8:
        hits.append(RuleHit("VELOCITY_10M", "8+ prior transactions in the last 10 minutes", 0.40))

    if f["amount_zscore"] >= 4 and t.amount >= 200:
        hits.append(RuleHit("AMOUNT_SPIKE", "Amount far above this account's normal spend", 0.40))

    if f["new_device"] and t.amount >= 1000:
        hits.append(RuleHit("NEW_DEVICE_HIGH_VALUE", "High-value spend from a never-seen device", 0.35))

    if f["country_mismatch"] and f["high_risk_mcc"] and t.amount >= 500:
        hits.append(RuleHit("FOREIGN_HIGH_RISK_MCC", "Foreign high-risk merchant category", 0.40))

    # Risk-based jurisdiction check on where the money goes / happens, never on the customer's nationality.
    for label, c in (("Counterparty", t.cp_country), ("Transaction", t.country)):
        if c and c in high_risk_countries:
            hits.append(RuleHit("HIGH_RISK_JURISDICTION", f"{label} country {c} is on the high-risk jurisdiction list", 0.50))
            break

    # Structuring: repeated amounts just below the reporting threshold within 24h.
    if f.get("near_threshold") and f.get("n_near_threshold_24h", 0) >= 2:
        hits.append(RuleHit("STRUCTURING", "Repeated amounts just under the reporting threshold in 24 hours", 0.55))

    if t.amount >= review_amount_usd:
        hits.append(RuleHit("AMOUNT_CEILING", f"Amount >= {review_amount_usd:,.0f} USD requires review", 0.55))

    return hits


def combine(hits: list[RuleHit]) -> float:
    """Noisy-OR of rule weights."""
    p = 1.0
    for h in hits:
        p *= 1.0 - min(h.weight, 0.999)
    return 1.0 - p
