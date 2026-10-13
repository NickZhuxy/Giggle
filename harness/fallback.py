"""Fail over to a second provider, guarded by a circuit breaker.

When the primary provider times out, rate-limits, or is down, the call goes
to the secondary provider instead. After repeated primary failures the
breaker opens and requests skip the primary entirely (no waiting for its
timeout) until a cooldown passes and one trial call succeeds again.
"""

import threading
import time

from .client import ProviderError

FAILOVER_KINDS = {"timeout", "rate_limited", "unavailable"}


class CircuitBreaker:
    """closed -> open after ``threshold`` consecutive failures;
    open -> half-open after ``cooldown_s``; one trial call decides."""

    def __init__(
        self, threshold: int = 5, cooldown_s: float = 30.0, clock=time.monotonic
    ):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if (
                self.state == "open"
                and self.clock() - self._opened_at >= self.cooldown_s
            ):
                self.state = "half_open"
            if self.state == "half_open" and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self):
        with self._lock:
            self.state = "closed"
            self._failures = 0
            self._trial_in_flight = False

    def record_failure(self):
        with self._lock:
            self._failures += 1
            self._trial_in_flight = False
            if self.state == "half_open" or self._failures >= self.threshold:
                self.state = "open"
                self._opened_at = self.clock()


class FallbackProvider:
    """A provider that tries ``primary`` and fails over to ``secondary``.

    The secondary call gets whatever time the primary left, but at least half
    of the original timeout, so one attempt takes at most 1.5x the timeout.
    """

    def __init__(
        self,
        primary,
        secondary,
        secondary_model,
        breaker=None,
        metrics=None,
        clock=time.monotonic,
    ):
        self.primary = primary
        self.secondary = secondary
        self.secondary_model = secondary_model
        self.breaker = breaker or CircuitBreaker()
        self.metrics = metrics
        self.clock = clock

    def _count(self, name):
        if self.metrics is not None:
            self.metrics.incr(name)

    def complete(self, prompt, *, model, max_tokens, timeout):
        start = self.clock()
        if self.breaker.allow():
            try:
                text, usage = self.primary.complete(
                    prompt, model=model, max_tokens=max_tokens, timeout=timeout
                )
                self.breaker.record_success()
                return text, {**usage, "model": usage.get("model", model)}
            except ProviderError as e:
                if e.kind not in FAILOVER_KINDS:
                    raise
                self.breaker.record_failure()
                self._count(f"primary_failed.{e.kind}")
        else:
            self._count("breaker_skipped_primary")
        self._count("fallback_calls")
        remaining = max(timeout * 0.5, timeout - (self.clock() - start))
        text, usage = self.secondary.complete(
            prompt, model=self.secondary_model, max_tokens=max_tokens, timeout=remaining
        )
        return text, {**usage, "model": self.secondary_model}
