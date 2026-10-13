# Giggle benchmarks

This page has the full results of the three benchmarks. For an overview, read the [README](../README.md).

All three benchmarks use simulated providers (`harness/fake.py`) with injected faults and latency. They show how each mechanism behaves under stated conditions. They are not measurements of the OpenAI API. The raw results for each seed are in [`bench/results/2026-10-13/`](../bench/results/2026-10-13/).

## 1. Request paths under faults

```sh
python -m bench.run_benchmark --out bench/results/latest
```

The same request trace goes through four request paths. Each path adds one mechanism to the path before it.

| Metric | A original | B harness | C + cache | D + coalescing |
| --- | ---: | ---: | ---: | ---: |
| Valid responses | 85.8% | 98.3% | 99.8% | 99.9% |
| All 5 results returned | 83.8% | 98.3% | 99.8% | 99.9% |
| Invalid output served to user | 11.8% | 0.0% | 0.0% | 0.0% |
| Errors returned | 2.4% | 1.7% | 0.2% | 0.1% |
| Provider calls per request | 0.23 | 1.30 | 0.29 | 0.17 |
| p95 latency (ms) | 110 | 346 | 173 | 154 |

Conditions: 400 requests over 60 distinct queries with repeats, 16 concurrent clients, mean of 3 seeds. For each provider call, the simulated provider adds 25% faults: timeouts 4%, rate limits 6%, prose instead of JSON 6%, truncated output 5%, and wrong fields 4%.

Findings:

- The original path cached any output that it could parse, including results with wrong fields. It then served that output again for 30 minutes.
- Retries make the harness correct, but they cost more calls (B). The cache removes most of that cost (C). Coalescing removes duplicate calls for popular queries: 41% fewer provider calls than C.
- The harness has a higher p95 latency than the original path, because a retry takes time.

## 2. Cache savings by repeat rate

```sh
python -m bench.cache_savings --out bench/results/latest
```

The harness runs with the cache off and on. Coalescing is off in both runs, so the table shows the effect of the cache only.

| Repeated requests in trace | Calls, cache off | Calls, cache on | Reduction |
| ---: | ---: | ---: | ---: |
| 53% | 1000 | 472 | 53% |
| 59% | 1000 | 420 | 58% |
| 67% | 1000 | 341 | 66% |
| 77% | 1000 | 240 | 76% |
| 90% | 1000 | 127 | 87% |

Conditions: 1,000 requests per trace, Zipf-like query popularity, 8 concurrent clients, no injected faults, seed 7. All repeats fall inside the 30-minute cache time-to-live (TTL).

Finding: the saving is almost equal to the share of repeated requests. The saving for a real deployment depends on its query mix.

## 3. Primary provider outage

```sh
python -m bench.outage --out bench/results/latest
```

The primary provider stops responding for 2 seconds in the middle of the run. Each call during the outage waits for the full timeout and then fails.

| Metric | No fallback | Fallback | Fallback + breaker |
| --- | ---: | ---: | ---: |
| Valid responses, whole run | 92.9% | 100.0% | 100.0% |
| Valid responses, during outage | 63.7% | 100.0% | 100.0% |
| p95 latency during outage (ms) | 1026 | 462 | 411 |
| Calls sent to the dead primary | 174 | 133 | 25 |

Conditions: 800 requests, 5 ms apart, 32 workers, mean of 3 seeds. Outage from 1.0 s to 3.0 s; timeout 0.3 s. The breaker opens after 5 consecutive failures and tries the primary again after 0.5 s.

Findings:

- Fallback keeps every response valid during the outage.
- The circuit breaker stops 81% of the calls to the dead primary. Requests do not wait for its timeout, so p95 latency is lower.

## 4. Multi-worker coalescing (Docker)

This check ran the full stack in demo mode with 4 gunicorn workers behind nginx. Twenty identical searches arrived at the same time.

| Result | Count |
| --- | ---: |
| Provider calls | 1 |
| Merged inside one worker process | 12 |
| Merged across worker processes (MongoDB lease) | 3 |
| Served from the cache | 4 |

## 5. Real-model prompt evaluation

```sh
python -m bench.prompt_eval --estimate
python -m bench.prompt_eval --yes --out bench/results/prompt-eval-<date>
```

This script compares three prompt versions on real OpenAI models (48 fixed queries in `bench/queries.txt`). It measures the strict JSON parse rate, the rate of valid results after the harness, latency, and cost per 1,000 searches. **It has not been run yet.** It needs an API key and costs about USD 4 at list prices.
