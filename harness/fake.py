"""A provider that needs no network, for tests and the benchmark.

``ScriptedProvider`` replays a fixed list of outcomes. ``FaultyProvider``
draws outcomes at random with configured fault rates and latency, from a
seeded generator so every benchmark run is repeatable.
"""

import json
import random
import threading
import time

from .client import ProviderError


class ScriptedProvider:
    """Each outcome is response text, or a ``ProviderError`` to raise."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, prompt, *, model, max_tokens, timeout):
        with self._lock:
            self.calls += 1
            outcome = (
                self.outcomes.pop(0)
                if self.outcomes
                else ProviderError("unavailable", "script exhausted")
            )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"prompt_tokens": 10, "completion_tokens": 50}


def good_results(query: str, n: int = 5) -> str:
    return json.dumps(
        [
            {
                "title": f"{query} result {i}",
                "snippet": f"Invented snippet {i}.",
                "url": f"https://example.com/{i}",
            }
            for i in range(1, n + 1)
        ]
    )


class FaultyProvider:
    """Simulated search provider with latency and injected faults.

    ``faults`` maps an outcome name to its probability:
    ``timeout``, ``rate_limited``, ``bad_json`` (prose, no array),
    ``truncated`` (cut mid-array), ``wrong_schema`` (missing fields).
    """

    def __init__(self, faults, *, latency_ms=(40, 120), seed=0, sleep=time.sleep):
        self.faults = faults
        self.latency_ms = latency_ms
        self.rng = random.Random(seed)
        self.sleep = sleep
        self.calls = 0
        self._lock = threading.Lock()

    def _draw(self):
        with self._lock:
            self.calls += 1
            roll = self.rng.random()
            latency = self.rng.uniform(*self.latency_ms) / 1000
        for name, p in self.faults.items():
            if roll < p:
                return name, latency
            roll -= p
        return "ok", latency

    def complete(self, prompt, *, model, max_tokens, timeout):
        outcome, latency = self._draw()
        query = prompt.rsplit('"', 2)[-2] if '"' in prompt else "query"
        if outcome == "timeout":
            self.sleep(min(timeout, latency * 3))
            raise ProviderError("timeout", "simulated timeout")
        self.sleep(latency)
        if outcome == "rate_limited":
            raise ProviderError("rate_limited", "simulated 429")
        text = good_results(query)
        if outcome == "bad_json":
            text = "Sure! Here are some results you might like."
        elif outcome == "truncated":
            text = text[: len(text) // 3]
        elif outcome == "wrong_schema":
            text = json.dumps([{"name": "x", "link": "y"}] * 5)
        return text, {"prompt_tokens": 60, "completion_tokens": 250}
