"""Tiny dependency-free Prometheus text exposition."""
from __future__ import annotations

import threading
from collections import defaultdict

BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.decisions: dict[str, int] = defaultdict(int)
        self.errors = 0
        self.duplicates = 0
        self._bucket = [0] * len(BUCKETS)
        self._count = 0
        self._sum = 0.0

    def observe(self, decision: str, seconds: float) -> None:
        with self._lock:
            self.decisions[decision] += 1
            self._count += 1
            self._sum += seconds
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    self._bucket[i] += 1

    def error(self) -> None:
        with self._lock:
            self.errors += 1

    def duplicate(self) -> None:
        with self._lock:
            self.duplicates += 1

    def render(self, model_loaded: bool, accounts_in_memory: int | None) -> str:
        with self._lock:
            out = ["# TYPE streamguard_decisions_total counter"]
            for d, c in sorted(self.decisions.items()):
                out.append(f'streamguard_decisions_total{{decision="{d}"}} {c}')
            out += ["# TYPE streamguard_errors_total counter", f"streamguard_errors_total {self.errors}",
                    "# TYPE streamguard_duplicates_total counter", f"streamguard_duplicates_total {self.duplicates}",
                    "# TYPE streamguard_score_seconds histogram"]
            for b, c in zip(BUCKETS, self._bucket):
                out.append(f'streamguard_score_seconds_bucket{{le="{b}"}} {c}')
            out += [f'streamguard_score_seconds_bucket{{le="+Inf"}} {self._count}',
                    f"streamguard_score_seconds_sum {self._sum:.6f}",
                    f"streamguard_score_seconds_count {self._count}",
                    "# TYPE streamguard_model_loaded gauge", f"streamguard_model_loaded {int(model_loaded)}",
                    ]
            if accounts_in_memory is not None:
                out += ["# TYPE streamguard_accounts_in_memory gauge",
                        f"streamguard_accounts_in_memory {accounts_in_memory}"]
        return "\n".join(out) + "\n"
