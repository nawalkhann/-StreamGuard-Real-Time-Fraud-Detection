"""Loaders that turn *your* labelled history into ``(Txn, label | None, tag)`` rows for training.

label: 1 fraud, 0 legitimate, None unknown (used for feature state only, never as a training example).
"""
from __future__ import annotations

import csv
import logging

from pydantic import ValidationError

from .config import Settings
from .schemas import Transaction
from .storage import make_storage

log = logging.getLogger(__name__)
OPTIONAL = ("lat", "lon", "device_id", "ip")


def _truthy(v: str) -> int:
    v = str(v).strip().lower()
    if v in ("1", "true", "t", "yes", "y", "fraud"):
        return 1
    if v in ("0", "false", "f", "no", "n", "legit", ""):
        return 0
    raise ValueError(f"unrecognised label {v!r}")


def read_labels(path: str) -> dict[str, int]:
    """CSV with columns transaction_id,is_fraud (e.g. a chargeback export)."""
    with open(path, newline="") as f:
        return {r["transaction_id"].strip(): _truthy(r["is_fraud"]) for r in csv.DictReader(f)}


def load_csv(path: str, settings: Settings) -> list:
    rows, bad = [], 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = {"transaction_id", "account_id", "amount", "merchant_id", "merchant_category", "country",
                   "channel", "timestamp", "is_fraud"} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV is missing required columns: {sorted(missing)} (see --write-sample for the format)")
        for i, rec in enumerate(reader, start=2):
            try:
                label = _truthy(rec.pop("is_fraud"))
                rec = {k: (None if k in OPTIONAL and v == "" else v) for k, v in rec.items() if v is not None}
                rec = {k: v for k, v in rec.items() if not (k == "currency" and not v)}
                t = Transaction.model_validate(rec).to_internal(settings.fx_rates)
                rows.append((t, label, "csv"))
            except (ValidationError, ValueError) as e:
                bad += 1
                if bad <= 5:
                    log.warning("row %d skipped: %s", i, str(e).splitlines()[0])
    if bad:
        log.warning("%d invalid rows skipped of %d", bad, bad + len(rows))
    rows.sort(key=lambda r: r[0].ts)
    return rows


def load_db(settings: Settings, label_file: str | None, assume_approved_legit: bool) -> list:
    """Labels from analyst dispositions; optional chargeback file overrides them.

    Transactions that were APPROVEd and never alerted are *assumed* legitimate unless
    ``assume_approved_legit`` is off (then they only feed feature state). Undetected fraud
    therefore pollutes the negatives: supply a chargeback file with --labels where you can.
    """
    overrides = read_labels(label_file) if label_file else {}
    rows = []
    db = make_storage(settings)
    try:
        for payload, _, status, decision in db.labelled_history():
            t = Transaction.model_validate(payload).to_internal(settings.fx_rates)
            if t.id in overrides:
                label = overrides[t.id]
            elif status == "confirmed_fraud":
                label = 1
            elif status == "false_positive":
                label = 0
            elif status is None and decision == "APPROVE" and assume_approved_legit:
                label = 0
            else:
                label = None  # open alert: outcome unknown
            rows.append((t, label, "db"))
    finally:
        db.close()
    return rows
