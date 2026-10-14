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
from .labelled import load_csv, load_db
from .synthetic import generate, write_csv

log = logging.getLogger("streamguard.train")


def build_dataset(rows, high_risk_mccs) -> tuple[np.ndarray, np.ndarray]:
    """Replay *all* rows in time order so feature state is complete; keep only labelled ones as examples."""
    store = FeatureStore(high_risk_mccs, max_accounts=10**9)
    X, y = [], []
    for t, label, _ in rows:
        f = store.observe(t)
        if label is not None:
            X.append([f[n] for n in FEATURE_NAMES])
            y.append(label)
    return np.asarray(X, dtype=np.float64), np.asarray(y, dtype=np.int8)


def threshold_table(y, p) -> list[dict]:
    out = []
    for thr in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95):
        pred = p >= thr
        tp = int((pred & (y == 1)).sum())
        out.append({"threshold": thr, "flagged": int(pred.sum()),
                    "precision": round(tp / max(pred.sum(), 1), 4), "recall": round(tp / max(int(y.sum()), 1), 4)})
    return out


def recommend(table: list[dict], review_precision: float, decline_precision: float) -> dict:
    """Lowest threshold meeting a precision target (maximises recall at that precision)."""
    def pick(target, above=0.0):
        ok = [r for r in table if r["precision"] >= target and r["flagged"] > 0 and r["threshold"] > above]
        return ok[0]["threshold"] if ok else None
    review = pick(review_precision)
    # decline must sit strictly above review (Settings enforces review < decline)
    return {"review_threshold": review, "decline_threshold": pick(decline_precision, above=review or 0.0)}


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


def train_from_rows(settings: Settings, rows, data_tag: str, seed: int = 7, min_positives: int = 200,
                    review_precision: float = 0.5, decline_precision: float = 0.9) -> FraudModel:
    X, y = build_dataset(rows, settings.high_risk_mcc_set)
    log.info("%d labelled examples, %d fraud (%.3f%%)", len(y), y.sum(), 100 * y.mean() if len(y) else 0)
    cut = int(len(y) * 0.75)  # chronological split: rows are time-sorted
    pos_train, pos_test = int(y[:cut].sum()), int(y[cut:].sum())
    if pos_train < min_positives or pos_test < 20:
        raise SystemExit(
            f"Refusing to train: {pos_train} fraud examples in the training window and {pos_test} in the hold-out "
            f"(need >= {min_positives} and >= 20). Too few positives gives an unreliable model; collect more labelled "
            "history or lower --min-positives knowingly.")
    clf = HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.08, max_leaf_nodes=24, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=seed)
    clf.fit(X[:cut], y[:cut])
    p_test = clf.predict_proba(X[cut:])[:, 1]
    metrics = evaluate(y[cut:], p_test, settings.review_threshold, settings.decline_threshold)
    metrics["threshold_table"] = threshold_table(y[cut:], p_test)
    metrics["recommended"] = recommend(metrics["threshold_table"], review_precision, decline_precision)
    metrics["data"] = data_tag

    clf.fit(X, y)  # refit on everything for the shipped artifact
    version = "gbm-" + hashlib.sha1(f"{seed}{len(y)}{time.time()}".encode()).hexdigest()[:8]
    model = FraudModel(clf=clf, feature_names=list(FEATURE_NAMES), baseline=np.median(X, axis=0),
                       version=version, metrics=metrics)
    model.save(settings.model_path)
    log.info("Saved %s -> %s", version, settings.model_path)
    return model


def train(settings: Settings, accounts: int, days: int, seed: int) -> FraudModel:
    """Bootstrap model on synthetic data."""
    rows = generate(n_accounts=accounts, days=days, seed=seed)
    return train_from_rows(settings, rows, "synthetic", seed, min_positives=20)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--csv", help="labelled transactions CSV (see --write-sample for the format)")
    src.add_argument("--from-db", action="store_true", help="use analyst dispositions in the StreamGuard database")
    src.add_argument("--write-sample", metavar="PATH", help="write a synthetic CSV showing the expected format, then exit")
    ap.add_argument("--labels", help="with --from-db: CSV transaction_id,is_fraud (e.g. chargebacks) overriding dispositions")
    ap.add_argument("--no-assume-approved-legit", action="store_true",
                    help="with --from-db: do not treat un-alerted approvals as legitimate")
    ap.add_argument("--min-positives", type=int, default=200)
    ap.add_argument("--review-precision", type=float, default=0.5, help="precision target for the REVIEW threshold")
    ap.add_argument("--decline-precision", type=float, default=0.9, help="precision target for the DECLINE threshold")
    ap.add_argument("--accounts", type=int, default=3000, help="synthetic bootstrap only")
    ap.add_argument("--days", type=int, default=45, help="synthetic bootstrap only")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    s = Settings()
    if a.write_sample:
        write_csv(generate(300, 10, a.seed), a.write_sample)
        print(f"wrote {a.write_sample}")
        return
    if a.csv or a.from_db:
        rows = load_csv(a.csv, s) if a.csv else load_db(s, a.labels, not a.no_assume_approved_legit)
        tag = f"csv:{a.csv}" if a.csv else "db"
        m = train_from_rows(s, rows, tag, a.seed, a.min_positives, a.review_precision, a.decline_precision)
    else:
        m = train(s, a.accounts, a.days, a.seed)
    print(json.dumps(m.metrics, indent=2))
    rec = m.metrics["recommended"]
    print("\nSuggested settings (verify on your cost model before applying):")
    for k, v in rec.items():
        print(f"  STREAMGUARD_{k.upper()}={v if v is not None else '(no threshold met the precision target; keep current)'}")


if __name__ == "__main__":
    main()
