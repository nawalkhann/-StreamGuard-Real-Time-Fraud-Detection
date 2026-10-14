from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from streamguard.api import create_app
from streamguard.config import Settings


def make_txn(i: int = 1, **kw) -> dict:
    base = {
        "transaction_id": f"tx-{i}", "account_id": "acct-1", "amount": 25.0, "currency": "USD",
        "merchant_id": "m-1", "merchant_category": "5411", "country": "US", "channel": "card_present",
        "timestamp": time.time(),
    }
    base.update(kw)
    if isinstance(base["timestamp"], (int, float)):
        base["timestamp"] = datetime.fromtimestamp(base["timestamp"], timezone.utc).isoformat()
    return base


@pytest.fixture
def settings(tmp_path):
    return Settings(api_keys="k1,k2", db_path=str(tmp_path / "t.db"), model_path=str(tmp_path / "none.joblib"),
                    _env_file=None)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), headers={"X-API-Key": "k1"}) as c:
        yield c
