"""Thread-safe counters for cache hits, provider calls, retries, and errors."""

import threading
from collections import Counter


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = Counter()

    def incr(self, name: str, n: int = 1):
        with self._lock:
            self._counts[name] += n

    def snapshot(self) -> dict:
        with self._lock:
            return dict(sorted(self._counts.items()))
