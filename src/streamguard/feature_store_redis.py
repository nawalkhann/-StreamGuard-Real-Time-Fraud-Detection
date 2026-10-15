"""Redis-backed feature state so any StreamGuard instance can score any account.

Per transaction: take a short per-account lock -> load state (or rebuild it from the database if missing)
-> compute features -> (caller persists the decision) -> write state back and release the lock atomically.

Correctness notes
* The database is the source of truth. Redis state is a cache that can be flushed at any time; a missing
  account is rebuilt from its stored transactions.
* The state write is fenced: a Lua script only writes if this caller still holds the lock. If the lock lease
  expired (a stalled instance) the write is refused and the key is deleted so the next request rebuilds it from
  the database, which already contains the transaction.
* Redis unavailable => scoring raises (fail closed); it never silently scores with empty history.
"""
from __future__ import annotations

import json
import logging
import random
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from typing import Callable

from .features import AccountState, Txn, _Event, apply_txn, compute_features

log = logging.getLogger(__name__)

# KEYS[1]=lock KEYS[2]=state  ARGV[1]=token ARGV[2]=state json ARGV[3]=ttl seconds
_WRITE_AND_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
  redis.call('del', KEYS[1])
  return 1
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


class LockTimeout(RuntimeError):
    pass


def dumps_state(st: AccountState) -> str:
    return json.dumps({
        "e": [[e.ts, e.amount, e.merchant, e.lat, e.lon] for e in st.events],
        "n": st.n, "m": st.mean, "q": st.m2, "x": st.max_ts,
        "d": sorted(st.devices), "mc": sorted(st.merchants), "c": dict(st.countries),
    }, separators=(",", ":"))


def loads_state(raw: str | bytes) -> AccountState:
    d = json.loads(raw)
    return AccountState(
        events=[_Event(*e) for e in d["e"]], n=d["n"], mean=d["m"], m2=d["q"], max_ts=d["x"],
        devices=set(d["d"]), merchants=set(d["mc"]), countries=Counter(d["c"]))


class _Session:
    def __init__(self, store: "RedisFeatureStore", st: AccountState):
        self._store, self.st, self.dirty = store, st, False

    def compute(self, t: Txn) -> dict[str, float]:
        s = self._store
        return compute_features(self.st, t, s.high_risk_mccs, s.struct_lo, s.struct_hi)

    def commit(self, t: Txn) -> None:
        apply_txn(self.st, t)
        self.dirty = True


class RedisFeatureStore:
    needs_warm_start = False

    def __init__(self, client, storage, decode: Callable[[dict], Txn | None], high_risk_mccs,
                 structuring_threshold: float = 10_000.0, lock_ms: int = 5000, ttl_days: int = 45,
                 history_days: int = 30):
        self.r = client
        self.storage = storage
        self.decode = decode
        self.high_risk_mccs = frozenset(high_risk_mccs)
        self.struct_lo, self.struct_hi = 0.8 * structuring_threshold, structuring_threshold
        self.lock_ms, self.ttl, self.history_days = lock_ms, int(ttl_days * 86400), history_days
        self._write = client.register_script(_WRITE_AND_RELEASE)
        self._release = client.register_script(_RELEASE)

    def describe(self) -> dict:
        return {"backend": "redis"}

    def accounts(self) -> int | None:
        return None  # not tracked: would need a key scan

    def _rebuild(self, account: str) -> AccountState:
        st = AccountState()
        since = time.time() - self.history_days * 86400
        n = 0
        for payload in self.storage.account_history(account, since):
            t = self.decode(payload)
            if t is not None:
                apply_txn(st, t)
                n += 1
        if n:
            log.info("rebuilt state for one account from %d stored transactions", n)
        return st

    @contextmanager
    def session(self, account: str):
        token = uuid.uuid4().hex
        lkey, skey = f"sg:lock:{account}", f"sg:acct:{account}"
        deadline = time.monotonic() + 2 * self.lock_ms / 1000
        while not self.r.set(lkey, token, nx=True, px=self.lock_ms):
            if time.monotonic() > deadline:
                raise LockTimeout(f"could not lock account within {2 * self.lock_ms} ms")
            time.sleep(random.uniform(0.001, 0.004))
        sess: _Session | None = None
        try:
            raw = self.r.get(skey)
            if raw is None:
                sess = _Session(self, self._rebuild(account))
                sess.dirty = True  # persist the rebuilt state even if this request commits nothing
            else:
                sess = _Session(self, loads_state(raw))
            yield sess
        except BaseException:
            if sess is not None and sess.dirty:
                self.r.delete(skey)  # state may be inconsistent with the database: force a rebuild
            self._release(keys=[lkey], args=[token])
            raise
        else:
            if sess.dirty:
                ok = self._write(keys=[lkey, skey], args=[token, dumps_state(sess.st), self.ttl])
                if not ok:
                    log.warning("account lock lease expired before state write; invalidating cached state")
                    self.r.delete(skey)
            else:
                self._release(keys=[lkey], args=[token])
