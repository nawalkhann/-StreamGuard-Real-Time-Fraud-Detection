"""ML scorer: gradient-boosted trees over the streaming features + local explanations."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np

from .features import FEATURE_NAMES

log = logging.getLogger(__name__)


@dataclass
class FraudModel:
    clf: object
    feature_names: list[str]
    baseline: np.ndarray  # per-feature median of training data (for explanations)
    version: str
    metrics: dict = field(default_factory=dict)

    def _row(self, feats: dict[str, float]) -> np.ndarray:
        return np.array([feats[n] for n in self.feature_names], dtype=np.float64)

    def predict(self, feats: dict[str, float]) -> float:
        return float(self.clf.predict_proba(self._row(feats)[None, :])[0, 1])

    def explain(self, feats: dict[str, float], top: int = 3) -> list[tuple[str, float]]:
        """Occlusion attribution: how much does the score drop if a feature is set to its typical value?"""
        x = self._row(feats)
        m = len(x)
        batch = np.tile(x, (m + 1, 1))
        for i in range(m):
            batch[i + 1, i] = self.baseline[i]
        p = self.clf.predict_proba(batch)[:, 1]
        deltas = p[0] - p[1:]
        order = np.argsort(-deltas)[:top]
        return [(self.feature_names[i], float(deltas[i])) for i in order if deltas[i] > 0.01]

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @staticmethod
    def load(path: str | Path) -> "FraudModel | None":
        """Only load artifacts you produced yourself: joblib is pickle-based."""
        path = Path(path)
        if not path.exists():
            return None
        m = joblib.load(path)
        if list(m.feature_names) != FEATURE_NAMES:
            log.error("Model %s was trained on a different feature set; ignoring it. Retrain.", path)
            return None
        return m
