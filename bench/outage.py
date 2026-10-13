"""What happens when the primary provider goes down for a while?

    python -m bench.outage --out bench/results/latest

The primary provider hangs until timeout for a window in the middle of the
run. Three request paths replay the same paced trace:

  no fallback        harness only
  fallback           fail over to a secondary provider on every primary failure
  fallback+breaker   as above, but an open circuit breaker skips the primary

All providers are simulated; the numbers show mechanism behavior only.
"""

import argparse
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench.run_benchmark import is_valid, make_trace
from harness.cache import MemoryCache
from harness.client import LLMClient, LLMError
from harness.fake import FaultyProvider, OutageProvider
from harness.fallback import CircuitBreaker, FallbackProvider
from harness.service import Generator

BASE_FAULTS = {"bad_json": 0.03, "truncated": 0.02}


def run(config, args, seed):
    primary = OutageProvider(
        FaultyProvider(BASE_FAULTS, latency_ms=(40, 120), seed=seed),
        args.outage_start,
        args.outage_end,
    )
    provider = primary
    if config != "no fallback":
        threshold = 5 if config == "fallback+breaker" else 10**9
        provider = FallbackProvider(
            primary,
            FaultyProvider(BASE_FAULTS, latency_ms=(60, 160), seed=seed + 100),
            "secondary",
            CircuitBreaker(threshold=threshold, cooldown_s=args.cooldown),
        )
    client = LLMClient(
        provider,
        max_attempts=3,
        timeout_s=args.timeout,
        deadline_s=args.deadline,
        backoff_base_s=0.05,
    )
    gen = Generator(client, MemoryCache(), search_model="primary")
    trace = make_trace(args.requests, args.unique, seed)
    records, lock = [], threading.Lock()

    def one(query):
        during = primary.in_outage()
        start = time.perf_counter()
        try:
            results, _ = gen.search(query)
            ok = is_valid(results)
        except LLMError:
            ok = False
        with lock:
            records.append((during, ok, (time.perf_counter() - start) * 1000))

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for query in trace:
            pool.submit(one, query)
            time.sleep(args.interval)  # paced arrivals so the outage sits mid-run

    def stats(rows):
        lat = [r[2] for r in rows]
        return {
            "requests": len(rows),
            "valid_rate": sum(r[1] for r in rows) / len(rows),
            "p95_ms": statistics.quantiles(lat, n=100)[94],
        }

    outage_rows = [r for r in records if r[0]]
    return {
        "all": stats(records),
        "during_outage": stats(outage_rows),
        "primary_calls_during_outage": primary.calls_during_outage,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=800)
    parser.add_argument("--unique", type=int, default=3000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--interval", type=float, default=0.005)
    parser.add_argument("--outage-start", type=float, default=1.0)
    parser.add_argument("--outage-end", type=float, default=3.0)
    parser.add_argument("--cooldown", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=0.3)
    parser.add_argument("--deadline", type=float, default=1.5)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--out", default="bench/results/latest")
    args = parser.parse_args()

    configs = ["no fallback", "fallback", "fallback+breaker"]
    raw = {c: [run(c, args, s) for s in args.seeds] for c in configs}

    def mean(c, *path):
        vals = []
        for r in raw[c]:
            for p in path:
                r = r[p]
            vals.append(r)
        return statistics.mean(vals)

    lines = [
        "| Metric | " + " | ".join(configs) + " |",
        "| --- |" + " ---: |" * len(configs),
        "| Valid responses, whole run | "
        + " | ".join(f"{mean(c, 'all', 'valid_rate'):.1%}" for c in configs)
        + " |",
        "| Valid responses, during outage | "
        + " | ".join(f"{mean(c, 'during_outage', 'valid_rate'):.1%}" for c in configs)
        + " |",
        "| p95 latency during outage (ms) | "
        + " | ".join(f"{mean(c, 'during_outage', 'p95_ms'):.0f}" for c in configs)
        + " |",
        "| Calls sent to the dead primary | "
        + " | ".join(f"{mean(c, 'primary_calls_during_outage'):.0f}" for c in configs)
        + " |",
        "",
        f"Mean of {len(args.seeds)} seeds. {args.requests} paced requests "
        f"({args.interval * 1000:.0f} ms apart, {args.concurrency} workers), "
        f"primary outage from {args.outage_start} s to {args.outage_end} s "
        f"(calls hang for the {args.timeout} s timeout). Breaker: opens after 5 "
        f"consecutive failures, {args.cooldown} s cooldown. Simulated providers.",
    ]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "outage.json").write_text(
        json.dumps({"args": vars(args), "runs": raw}, indent=2)
    )
    (out / "outage.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
