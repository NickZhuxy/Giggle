| Metric | no fallback | fallback | fallback+breaker |
| --- | ---: | ---: | ---: |
| Valid responses, whole run | 92.9% | 100.0% | 100.0% |
| Valid responses, during outage | 63.7% | 100.0% | 100.0% |
| p95 latency during outage (ms) | 1026 | 462 | 411 |
| Calls sent to the dead primary | 174 | 133 | 25 |

Mean of 3 seeds. 800 paced requests (5 ms apart, 32 workers), primary outage from 1.0 s to 3.0 s (calls hang for the 0.3 s timeout). Breaker: opens after 5 consecutive failures, 0.5 s cooldown. Simulated providers.
