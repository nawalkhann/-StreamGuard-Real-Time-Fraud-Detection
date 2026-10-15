"""Optional real-infrastructure fixtures. Set these to run the Postgres/Redis tests (CI does):
   STREAMGUARD_TEST_PG_URL=postgresql://sg@127.0.0.1:5433/streamguard_test
   STREAMGUARD_TEST_REDIS_URL=redis://127.0.0.1:6390/0
"""
import os

import pytest

PG_URL = os.environ.get("STREAMGUARD_TEST_PG_URL", "")
REDIS_URL = os.environ.get("STREAMGUARD_TEST_REDIS_URL", "")

needs_pg = pytest.mark.skipif(not PG_URL, reason="STREAMGUARD_TEST_PG_URL not set")
needs_redis = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="PG/Redis test URLs not set")


def reset_pg() -> None:
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS alerts, blocklist, transactions CASCADE")


def reset_redis() -> None:
    import redis
    redis.Redis.from_url(REDIS_URL).flushdb()
