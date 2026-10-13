| Metric | A original | B harness | C + cache | D + coalescing |
| --- | ---: | ---: | ---: | ---: |
| Valid responses | 85.8% | 98.3% | 99.8% | 99.9% |
| All 5 results returned | 83.8% | 98.3% | 99.8% | 99.9% |
| Invalid output served to user | 11.8% | 0.0% | 0.0% | 0.0% |
| Errors returned | 2.4% | 1.7% | 0.2% | 0.1% |
| Provider calls per request | 0.23 | 1.30 | 0.29 | 0.17 |
| p50 latency (ms) | 0.0 | 96.2 | 0.0 | 0.0 |
| p95 latency (ms) | 110 | 346 | 173 | 154 |

Mean of 3 seeds (1, 2, 3); 400 requests per run over 60 distinct queries (Zipf-like repeats), 16 concurrent clients.
Injected faults per provider call: timeout 4%, rate_limited 6%, bad_json 6%, truncated 5%, wrong_schema 4%. Simulated latency 40-120 ms. These are simulated conditions, not OpenAI API measurements. Min/max per seed are in results.json.
