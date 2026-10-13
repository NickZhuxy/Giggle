"""Replay one search trace through four request paths and compare them.

    python -m bench.run_benchmark --out bench/results/latest

A  original  - the pre-harness code path: one call, safe_parse_json, 30-min cache
B  harness   - shared client: validation + bounded retries, no cache
C  + cache   - B with the version-aware cache
D  + flight  - C with same-key request coalescing

Faults and latency are simulated (see harness/fake.py), so the numbers show
how each mechanism behaves under the configured fault mix. They are not
measurements of the real OpenAI API.
"""

import argparse
import json
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from app import safe_parse_json
from harness.cache import MemoryCache, normalize_query
from harness.client import LLMClient, LLMError, ProviderError
from harness.fake import FaultyProvider
from harness.metrics import Metrics
from harness.service import SEARCH_PROMPT, Generator
from harness.validation import _valid_result

FAULTS = {
    "timeout": 0.04,
    "rate_limited": 0.06,
    "bad_json": 0.06,
    "truncated": 0.05,
    "wrong_schema": 0.04,
}


def make_trace(n_requests, n_unique, seed):
    """Zipf-like popularity: a few queries repeat a lot, most appear rarely."""
    rng = random.Random(seed)
    queries = [f"query number {i}" for i in range(n_unique)]
    weights = [1 / (rank + 1) ** 1.1 for rank in range(n_unique)]
    return rng.choices(queries, weights=weights, k=n_requests)


def is_valid(results):
    return (
        isinstance(results, list) and bool(results) and all(map(_valid_result, results))
    )


class OriginalPath:
    """The request path before the harness, kept faithful for comparison."""

    def __init__(self, provider, timeout_s):
        self.provider = provider
        self.timeout_s = timeout_s
        self.cache = MemoryCache()
        self.metrics = Metrics()

    def search(self, query):
        cached = self.cache.get(query)
        if cached:
            self.metrics.incr("cache_hit")
            return cached
        self.metrics.incr("provider_calls")
        try:
            text, _ = self.provider.complete(
                SEARCH_PROMPT.format(query=query),
                model="gpt-4",
                max_tokens=500,
                timeout=self.timeout_s,
            )
        except ProviderError as e:
            raise LLMError(e.kind, str(e), 1) from e
        try:
            results = safe_parse_json(text)
            self.cache.set(query, results, ttl_s=1800)  # caches whatever parsed
        except ValueError as e:
            results = [{"title": "Parse Error", "snippet": str(e), "url": "#"}]
        return results


def build(config, seed, args):
    provider = FaultyProvider(FAULTS, latency_ms=(40, 120), seed=seed)
    if config == "A":
        return OriginalPath(provider, args.timeout), provider
    metrics = Metrics()
    client = LLMClient(
        provider,
        max_attempts=3,
        timeout_s=args.timeout,
        deadline_s=args.deadline,
        backoff_base_s=0.05,
        metrics=metrics,
        rng=random.Random(seed),
    )

    class NoCache(MemoryCache):
        def get(self, key):
            return None

    cache = NoCache() if config == "B" else MemoryCache()
    gen = Generator(client, cache, coalesce=(config == "D"), metrics=metrics)
    gen.metrics_ref = metrics
    return gen, provider


def run_once(config, seed, args):
    target, provider = build(config, seed, args)
    trace = make_trace(args.requests, args.unique, seed)
    latencies, outcomes = [], []
    lock = threading.Lock()

    def one(query):
        start = time.perf_counter()
        try:
            out = target.search(query)
            results = out[0] if isinstance(out, tuple) else out
            ok = "valid" if is_valid(results) else "invalid"
            if ok == "valid" and len(results) == 5:
                ok = "valid_full"
        except LLMError as e:
            ok = f"error:{e.kind}"
        with lock:
            latencies.append((time.perf_counter() - start) * 1000)
            outcomes.append(ok)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(one, trace))

    metrics = (target.metrics if config == "A" else target.metrics_ref).snapshot()
    n = len(trace)
    qs = statistics.quantiles(latencies, n=100)
    return {
        "valid_rate": sum(o.startswith("valid") for o in outcomes) / n,
        "full_rate": outcomes.count("valid_full") / n,
        "invalid_served_rate": outcomes.count("invalid") / n,
        "error_rate": sum(o.startswith("error") for o in outcomes) / n,
        "provider_calls_per_request": provider.calls / n,
        "retries": metrics.get("retries", 0),
        "cache_hits": metrics.get("cache_hit", 0),
        "coalesced": metrics.get("coalesced", 0),
        "p50_ms": qs[49],
        "p95_ms": qs[94],
    }


def summarize(runs):
    keys = runs[0].keys()
    return {
        k: {
            "mean": statistics.mean(r[k] for r in runs),
            "min": min(r[k] for r in runs),
            "max": max(r[k] for r in runs),
        }
        for k in keys
    }


def markdown(summary, args):
    rows = [
        ("Valid responses", "valid_rate", "{:.1%}"),
        ("All 5 results returned", "full_rate", "{:.1%}"),
        ("Invalid output served to user", "invalid_served_rate", "{:.1%}"),
        ("Errors returned", "error_rate", "{:.1%}"),
        ("Provider calls per request", "provider_calls_per_request", "{:.2f}"),
        ("p50 latency (ms)", "p50_ms", "{:.1f}"),
        ("p95 latency (ms)", "p95_ms", "{:.0f}"),
    ]
    names = {
        "A": "A original",
        "B": "B harness",
        "C": "C + cache",
        "D": "D + coalescing",
    }
    lines = [
        "| Metric | " + " | ".join(names[c] for c in summary) + " |",
        "| --- |" + " ---: |" * len(summary),
    ]
    for label, key, fmt in rows:
        cells = [fmt.format(summary[c][key]["mean"]) for c in summary]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    fault_text = ", ".join(f"{k} {v:.0%}" for k, v in FAULTS.items())
    lines += [
        "",
        f"Mean of {len(args.seeds)} seeds ({', '.join(map(str, args.seeds))}); "
        f"{args.requests} requests per run over {args.unique} distinct queries "
        f"(Zipf-like repeats), {args.concurrency} concurrent clients.",
        f"Injected faults per provider call: {fault_text}. "
        "Simulated latency 40-120 ms. These are simulated conditions, not "
        "OpenAI API measurements. Min/max per seed are in results.json.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--unique", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=0.3)
    parser.add_argument("--deadline", type=float, default=1.5)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--out", default=f"bench/results/{date.today().isoformat()}")
    args = parser.parse_args()

    summary, raw = {}, {}
    for config in "ABCD":
        raw[config] = [run_once(config, seed, args) for seed in args.seeds]
        summary[config] = summarize(raw[config])
        print(config, {k: round(v["mean"], 3) for k, v in summary[config].items()})

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps(
            {"args": vars(args), "faults": FAULTS, "summary": summary, "runs": raw},
            indent=2,
        )
    )
    (out / "summary.md").write_text(markdown(summary, args))
    print(f"wrote {out}/results.json and {out}/summary.md")


if __name__ == "__main__":
    main()
