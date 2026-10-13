"""Cost and abuse guardrails: token pricing, a daily budget, and rate limits.

Memory implementations serve tests and the benchmark. Mongo implementations
share state across gunicorn workers through atomic ``$inc`` updates.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

# USD per 1M tokens: (input, output). Prices change; override them with the
# GIGGLE_PRICES_JSON environment variable, e.g. '{"gpt-4o": [2.5, 10]}'.
# OpenAI entries are list prices known at implementation time: verify
# them on the provider's pricing page before relying on cost reports.
DEFAULT_PRICES = {
    "gpt-4": (30.0, 60.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "deepseek-flash": (0.15, 0.6),  # off-peak, checked 2026-10-08
}


def load_prices() -> dict:
    prices = dict(DEFAULT_PRICES)
    override = os.getenv("GIGGLE_PRICES_JSON")
    if override:
        prices.update({k: tuple(v) for k, v in json.loads(override).items()})
    return prices


def cost_usd(model: str, usage: dict, prices=None) -> float:
    """Cost of one call; unknown models cost 0 and should be added to prices."""
    rate_in, rate_out = (prices or DEFAULT_PRICES).get(model, (0.0, 0.0))
    return (
        usage.get("prompt_tokens", 0) * rate_in
        + usage.get("completion_tokens", 0) * rate_out
    ) / 1_000_000


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class MemoryBudget:
    """Daily spending limit for one process."""

    def __init__(self, daily_usd: float, today=_today):
        self.daily_usd = daily_usd
        self.today = today
        self._spent = {}
        self._lock = threading.Lock()

    def spent(self) -> float:
        with self._lock:
            return self._spent.get(self.today(), 0.0)

    def allow(self) -> bool:
        return self.spent() < self.daily_usd

    def add(self, usd: float):
        with self._lock:
            day = self.today()
            self._spent[day] = self._spent.get(day, 0.0) + usd


class MongoBudget:
    """Daily spending limit shared by every worker (one document per day).

    Calls that are already in flight when the limit is reached still finish,
    so the day's spend can exceed the limit by at most those calls.
    """

    def __init__(self, collection, daily_usd: float, today=_today):
        self.col = collection
        self.daily_usd = daily_usd
        self.today = today

    def spent(self) -> float:
        doc = self.col.find_one({"_id": f"budget:{self.today()}"})
        return doc["usd"] if doc else 0.0

    def allow(self) -> bool:
        return self.spent() < self.daily_usd

    def add(self, usd: float):
        day = self.today()
        self.col.update_one(
            {"_id": f"budget:{day}"},
            {
                "$inc": {"usd": usd},
                "$setOnInsert": {
                    "expires_at": datetime.now(timezone.utc) + timedelta(days=7)
                },
            },
            upsert=True,
        )


class MemoryRateLimiter:
    """Fixed-window request counter per key (for example, per client IP)."""

    def __init__(self, limit: int, window_s: int = 60, clock=time.time):
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._counts = {}
        self._lock = threading.Lock()

    def hit(self, key: str):
        """Count one request; return ``(allowed, retry_after_seconds)``."""
        now = self.clock()
        window = int(now // self.window_s)
        with self._lock:
            self._counts = {k: v for k, v in self._counts.items() if k[1] == window}
            count = self._counts.get((key, window), 0) + 1
            self._counts[(key, window)] = count
        retry_after = int(self.window_s - now % self.window_s) + 1
        return count <= self.limit, retry_after


class MongoRateLimiter:
    """Fixed-window counter shared by every worker; a TTL index cleans up."""

    def __init__(self, collection, limit: int, window_s: int = 60, clock=time.time):
        self.col = collection
        self.limit = limit
        self.window_s = window_s
        self.clock = clock

    def hit(self, key: str):
        now = self.clock()
        window = int(now // self.window_s)
        doc = self.col.find_one_and_update(
            {"_id": f"rate:{key}:{window}"},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {
                    "expires_at": datetime.fromtimestamp(
                        (window + 2) * self.window_s, timezone.utc
                    )
                },
            },
            upsert=True,
            return_document=True,
        )
        retry_after = int(self.window_s - now % self.window_s) + 1
        return doc["count"] <= self.limit, retry_after
