"""Version-aware caching and same-key request coalescing.

A cache key covers everything that can change the output: the normalized
input, the model, the prompt version, and generation parameters. Bumping
``PROMPT_VERSION`` therefore retires every old entry without a migration.
"""

import hashlib
import os
import uuid
import json
import threading
import time
from datetime import datetime, timedelta, timezone


def normalize_query(query: str) -> str:
    return " ".join(query.split()).casefold()


def cache_key(
    kind: str, payload: str, *, model: str, prompt_version: str, params=None
) -> str:
    material = json.dumps(
        {
            "kind": kind,
            "payload": payload,
            "model": model,
            "prompt_version": prompt_version,
            "params": params or {},
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


class MemoryCache:
    """In-process cache with optional TTL; used by tests and the benchmark."""

    def __init__(self, clock=time.monotonic):
        self._data = {}
        self._lock = threading.Lock()
        self.clock = clock

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires = entry
            if expires is not None and self.clock() >= expires:
                del self._data[key]
                return None
            return value

    def set(self, key, value, ttl_s=None, **_meta):
        expires = None if ttl_s is None else self.clock() + ttl_s
        with self._lock:
            self._data[key] = (value, expires)


class MongoCache:
    """Cache documents in one MongoDB collection, keyed by ``_id``.

    Expired entries are ignored on read; a TTL index on ``expires_at``
    lets MongoDB delete them in the background.
    """

    def __init__(self, collection):
        self.col = collection

    def ensure_indexes(self):
        self.col.create_index("expires_at", expireAfterSeconds=0)

    def get(self, key):
        doc = self.col.find_one({"_id": key})
        if doc is None:
            return None
        expires = doc.get("expires_at")
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(timezone.utc):
                return None
        return doc["value"]

    def set(self, key, value, ttl_s=None, **meta):
        now = datetime.now(timezone.utc)
        doc = {"value": value, "created_at": now, **meta}
        if ttl_s is not None:
            doc["expires_at"] = now + timedelta(seconds=ttl_s)
        self.col.replace_one({"_id": key}, doc, upsert=True)


class SingleFlight:
    """Run one call per key at a time; concurrent callers share its result.

    This merges duplicate requests inside one process only. Separate worker
    processes still each make their own call.
    """

    class _Call:
        def __init__(self):
            self.done = threading.Event()
            self.result = None
            self.error = None

    def __init__(self):
        self._lock = threading.Lock()
        self._calls = {}

    def do(self, key, fn):
        """Return ``(result, shared)``; ``shared`` is True for followers."""
        with self._lock:
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = self._calls[key] = self._Call()
        if not leader:
            call.done.wait()
            if call.error is not None:
                raise call.error
            return call.result, True
        try:
            call.result = fn()
            return call.result, False
        except Exception as e:
            call.error = e
            raise
        finally:
            with self._lock:
                del self._calls[key]
            call.done.set()


class MongoLease:
    """Cross-process single-flight: one worker generates a key, others wait.

    ``acquire`` inserts a lease document whose ``_id`` is the cache key; the
    unique ``_id`` makes the insert succeed for exactly one worker. A lease
    expires after ``ttl_s`` so a crashed worker cannot block a key forever.
    """

    def __init__(
        self, collection, ttl_s=60.0, poll_s=0.05, wait_s=50.0, sleep=time.sleep
    ):
        self.col = collection
        self.ttl_s = ttl_s
        self.poll_s = poll_s
        self.wait_s = wait_s
        self.sleep = sleep
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def acquire(self, key) -> bool:
        from pymongo.errors import DuplicateKeyError

        now = datetime.now(timezone.utc)
        self.col.delete_one({"_id": key, "expires_at": {"$lte": now}})
        try:
            self.col.insert_one(
                {
                    "_id": key,
                    "owner": self.owner,
                    "expires_at": now + timedelta(seconds=self.ttl_s),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    def release(self, key):
        self.col.delete_one({"_id": key, "owner": self.owner})

    def wait_for(self, key, lookup):
        """Poll ``lookup(key)`` until it returns a value or the lease ends."""
        waited = 0.0
        while waited < self.wait_s:
            value = lookup(key)
            if value is not None:
                return value
            if self.col.find_one({"_id": key}) is None:
                return lookup(key)  # leader finished (or failed) since last poll
            self.sleep(self.poll_s)
            waited += self.poll_s
        return None
