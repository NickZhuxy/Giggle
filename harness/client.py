"""One place for every LLM call: timeouts, a total deadline, and bounded retries.

The client only sees ``ProviderError`` and plain text. Provider adapters
(OpenAI, or the fake provider used by tests and the benchmark) translate
their own exceptions into a ``ProviderError`` with a ``kind``.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# Error kinds that can succeed if we simply ask again.
RETRYABLE = {"timeout", "rate_limited", "unavailable", "invalid_output"}


class ProviderError(Exception):
    """A failed provider call, classified by ``kind``."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


class LLMError(Exception):
    """The client gave up. ``attempts`` counts every provider call made."""

    def __init__(self, kind: str, message: str, attempts: int):
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


@dataclass
class Completion:
    value: object
    attempts: int
    latency_ms: float
    usage: dict = field(default_factory=dict)


class OpenAIProvider:
    """Adapter from the OpenAI SDK to ``ProviderError`` kinds."""

    def __init__(self, sdk_client):
        self.sdk = sdk_client

    def complete(self, prompt: str, *, model: str, max_tokens: int, timeout: float):
        import openai

        try:
            resp = self.sdk.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except openai.APITimeoutError as e:
            raise ProviderError("timeout", str(e)) from e
        except openai.RateLimitError as e:
            raise ProviderError("rate_limited", str(e)) from e
        except (openai.APIConnectionError, openai.InternalServerError) as e:
            raise ProviderError("unavailable", str(e)) from e
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise ProviderError("auth", str(e)) from e
        except openai.BadRequestError as e:
            raise ProviderError("bad_request", str(e)) from e
        usage = getattr(resp, "usage", None)
        return resp.choices[0].message.content or "", {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
        }


class LLMClient:
    """Calls the provider, validates the text, and retries within limits.

    ``max_attempts`` caps provider calls per request; ``deadline_s`` caps the
    total time including backoff, so retries can never extend a request
    without bound.
    """

    def __init__(
        self,
        provider,
        *,
        max_attempts: int = 3,
        timeout_s: float = 20.0,
        deadline_s: float = 45.0,
        backoff_base_s: float = 0.5,
        metrics=None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ):
        self.provider = provider
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self.deadline_s = deadline_s
        self.backoff_base_s = backoff_base_s
        self.metrics = metrics
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()

    def _count(self, name, n=1):
        if self.metrics is not None:
            self.metrics.incr(name, n)

    def complete(self, prompt, *, model, max_tokens, validate=lambda text: text):
        """Return a ``Completion`` whose ``value`` is ``validate(text)``.

        ``validate`` raises ``ValueError`` for unusable output; that counts as
        an ``invalid_output`` failure and is retried like a transient error.
        """
        start = self.clock()
        last = ProviderError("unavailable", "no attempt made")
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        for attempt in range(1, self.max_attempts + 1):
            remaining = self.deadline_s - (self.clock() - start)
            if remaining <= 0:
                last = ProviderError("timeout", "request deadline exceeded")
                break
            self._count("provider_calls")
            if attempt > 1:
                self._count("retries")
            try:
                text, call_usage = self.provider.complete(
                    prompt,
                    model=model,
                    max_tokens=max_tokens,
                    timeout=min(self.timeout_s, remaining),
                )
                for k, v in call_usage.items():
                    usage[k] = usage.get(k, 0) + (v or 0)
                value = validate(text)
                latency = (self.clock() - start) * 1000
                return Completion(value, attempt, latency, usage)
            except ValueError as e:
                self._count("invalid_outputs")
                last = ProviderError("invalid_output", str(e))
            except ProviderError as e:
                self._count(f"provider_error.{e.kind}")
                last = e
                if e.kind not in RETRYABLE:
                    raise LLMError(e.kind, str(e), attempt) from e
            if attempt < self.max_attempts:
                # Full-jitter exponential backoff, never past the deadline.
                delay = self.rng.uniform(0, self.backoff_base_s * 2 ** (attempt - 1))
                remaining = self.deadline_s - (self.clock() - start)
                self.sleep(max(0.0, min(delay, remaining)))
        raise LLMError(last.kind, str(last), attempt)
