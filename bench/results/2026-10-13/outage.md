| Metric | no fallback | fallback | fallback+breaker |
| --- | ---: | ---: | ---: |
| Valid responses, whole run | 92.8% | 100.0% | 100.0% |
| Valid responses, during outage | 64.3% | 100.0% | 100.0% |
| p95 latency during outage (ms) | 1027 | 459 | 413 |
| Calls sent to the dead primary | 175 | 134 | 24 |

Mean of 3 seeds. 800 paced requests (5 ms apart, 32 workers), primary outage from 1.0 s to 3.0 s (calls hang for the 0.3 s timeout). Breaker: opens after 5 consecutive failures, 0.5 s cooldown. Simulated providers.
