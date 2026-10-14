"""Train the fraud model: ``python -m streamguard.train``.

Features come from the same FeatureStore used in production, replayed in event
order, so training matches serving. Evaluation is a chronological hold-out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from .config import Settings
from .features import FEATURE_NAMES, FeatureStore
from .model import FraudModel
from .synthetic import generate

log = logging.getLogger("streamguard.train")


def build_dataset(rows, high_risk_mccs) -> tuple[np.ndarray, np.ndarray]:
    store = FeatureStore(high_risk_mccs, max_accounts=10**9)
    X = np.empty((len(rows), len(FEATURE_NAMES)))
    y = np.empty(len(rows), dtype=np.int8)
    for i, (t, label, _) in enumerate(rows):
        f = store.observe(t)
        X[i] = [f[n] for n in FEATURE_NAMES]
        y[i] = label
    return X, y


def evaluate(y, p, review: float, decline: float) -> dict:
    out = {"roc_auc": round(float(roc_auc_score(y, p)), 4),
           "avg_precision": round(float(average_precision_score(y, p)), 4),
           "fraud_rate": round(float(y.mean()), 4), "n": int(len(y))}
    for name, thr in (("review", review), ("decline", decline)):
        pred = p >= thr
        tp = int((pred & (y == 1)).sum())
        out[name] = {
            "threshold": thr, "flagged": int(pred.sum()),
            "precision": round(tp / max(pred.sum(), 1), 4), "recall": round(tp / max(int(y.sum()), 1), 4),
        }
    return out


def train(settings: Settings, accounts: int, days: int, seed: int) -> FraudModel:
    t0 = time.time()
    rows = generate(n_accounts=accounts, days=days, seed=seed)
    X, y = build_dataset(rows, settings.high_risk_mcc_set)
    log.info("Generated %d transactions (%.2f%% fraud) in %.1fs", len(y), 100 * y.mean(), time.time() - t0)

    cut = int(len(y) * 0.75)  # chronological split: rows are time-sorted
    clf = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.08, max_leaf_nodes=24, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=seed)
    clf.fit(X[:cut], y[:cut])
    metrics = evaluate(y[cut:], clf.predict_proba(X[cut:])[:, 1], settings.review_threshold, settings.decline_threshold)
    metrics["data"] = "synthetic"

    clf.fit(X, y)  # refit on everything for the shipped artifact
    version = "gbm-" + hashlib.sha1(f"{seed}{len(y)}{time.time()}".encode()).hexdigest()[:8]
    model = FraudModel(clf=clf, feature_names=list(FEATURE_NAMES), baseline=np.median(X, axis=0),
                       version=version, metrics=metrics)
    model.save(settings.model_path)
    log.info("Saved %s -> %s", version, settings.model_path)
    return model


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--accounts", type=int, default=3000)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    m = train(Settings(), a.accounts, a.days, a.seed)
    print(json.dumps(m.metrics, indent=2))


if __name__ == "__main__":
    main()
