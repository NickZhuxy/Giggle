"""How many provider calls does the cache save, as a function of repeats?

    python -m bench.cache_savings --out bench/results/latest

Replays traces with different shares of repeated queries through the
harness with the cache off and on (no injected faults, coalescing off), and reports the
reduction in provider calls. Savings depend on the workload: they are close
to the share of requests that repeat a query seen within the cache TTL.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bench.run_benchmark import make_trace
from harness.cache import MemoryCache
from harness.client import LLMClient
from harness.fake import FaultyProvider
from harness.service import Generator


class NoCache(MemoryCache):
    def get(self, key):
        return None


def provider_calls(trace, cache, concurrency, seed):
    provider = FaultyProvider({}, latency_ms=(5, 15), seed=seed)
    gen = Generator(LLMClient(provider), cache, coalesce=False)  # isolate the cache
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(gen.search, trace))
    return provider.calls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument(
        "--unique", type=int, nargs="+", default=[200000, 20000, 3000, 600, 120]
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="bench/results/latest")
    args = parser.parse_args()

    rows = []
    for unique in args.unique:
        trace = make_trace(args.requests, unique, args.seed)
        repeat_share = 1 - len(set(trace)) / len(trace)
        off = provider_calls(trace, NoCache(), args.concurrency, args.seed)
        on = provider_calls(trace, MemoryCache(), args.concurrency, args.seed)
        rows.append(
            {
                "distinct_query_pool": unique,
                "repeat_share": repeat_share,
                "calls_cache_off": off,
                "calls_cache_on": on,
                "reduction": 1 - on / off,
            }
        )
        print(rows[-1])

    lines = (
        [
            "| Repeated requests in trace | Calls, cache off | Calls, cache on | Reduction |",
            "| ---: | ---: | ---: | ---: |",
        ]
        + [
            f"| {r['repeat_share']:.0%} | {r['calls_cache_off']} | {r['calls_cache_on']} | {r['reduction']:.0%} |"
            for r in rows
        ]
        + [
            "",
            f"{args.requests} requests per trace, Zipf-like query popularity, "
            f"{args.concurrency} concurrent clients, no injected faults, seed {args.seed}. "
            "All repeats fall inside the 30-minute cache TTL in this replay.",
        ]
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cache_savings.json").write_text(
        json.dumps({"args": vars(args), "rows": rows}, indent=2)
    )
    (out / "cache_savings.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
