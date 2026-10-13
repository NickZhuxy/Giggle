| Repeated requests in trace | Calls, cache off | Calls, cache on | Reduction |
| ---: | ---: | ---: | ---: |
| 53% | 1000 | 472 | 53% |
| 59% | 1000 | 420 | 58% |
| 67% | 1000 | 341 | 66% |
| 77% | 1000 | 240 | 76% |
| 90% | 1000 | 127 | 87% |

1000 requests per trace, Zipf-like query popularity, 8 concurrent clients, no injected faults, seed 7. All repeats fall inside the 30-minute cache TTL in this replay.
