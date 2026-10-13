"""Prometheus metrics and structured request logs.

With several gunicorn workers, set ``PROMETHEUS_MULTIPROC_DIR`` so every
worker writes its metrics to a shared directory and ``/metrics`` reports the
sum (gunicorn.conf.py prepares the directory).
"""

import json
import logging
import os
import sys

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)

from .metrics import Metrics

EVENTS = Counter(
    "giggle_events_total",
    "Harness events: cache hits, provider calls, retries, errors, fallbacks",
    ["event"],
)
LLM_COST = Counter("giggle_llm_cost_usd_total", "Estimated LLM spend in USD")
REQUEST_SECONDS = Histogram(
    "giggle_http_request_duration_seconds",
    "HTTP request latency",
    ["route", "status"],
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)


class PrometheusMetrics(Metrics):
    """The in-process counters, mirrored to Prometheus."""

    def incr(self, name: str, n: int = 1):
        super().incr(name, n)
        if name == "cost_usd":
            LLM_COST.inc(n)
        else:
            EVENTS.labels(event=name).inc(n)


def observe_request(route: str, status: int, seconds: float):
    REQUEST_SECONDS.labels(route=route, status=str(status)).observe(seconds)


def prometheus_payload():
    """Return ``(body, content_type)`` for the ``/metrics`` endpoint."""
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST


class JsonFormatter(logging.Formatter):
    """One JSON object per line; extra fields come from ``record.fields``."""

    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, default=str)


def request_logger() -> logging.Logger:
    logger = logging.getLogger("giggle.requests")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
