# Performance Baseline (captured)

A real, reproducible baseline captured with the in-repo pure-Python load
generator ([`scripts/loadgen.py`](../scripts/loadgen.py)). Re-capture on your own
hardware before trusting these numbers - they exist to catch **regressions**, not
to advertise absolute peak throughput.

## Environment

| Field | Value |
| --- | --- |
| Host | Apple Silicon (arm64), 18 logical cores |
| OS | macOS 26.4.1 |
| Python | 3.12 |
| Server | single process (`EVOAGENT_WEB_WORKERS=1`), `ThreadingHTTPServer` |
| Store | SQLite (temp db) |
| Queue | `memory-ephemeral` |
| Auth | disabled |
| Load generator | co-located on the same host (indicative, not isolated) |

> Co-locating the generator with the server understates achievable throughput
> and inflates the tail; treat these as a conservative floor.

## Results

### Read mix (`steady`) - 300 req/s, 20s

| Metric | Value |
| --- | --- |
| Throughput | 300 req/s (sustained, no shedding) |
| Errors | 0 |
| p50 / p95 / p99 / p99.9 | 0.6 / 0.9 / 1.1 / 1.9 ms |
| max | 6.7 ms |

### Read mix (`steady`) - 1500 req/s, 15s

| Metric | Value |
| --- | --- |
| Throughput | 1500 req/s (sustained, no shedding) |
| Errors | 0 |
| p50 / p95 / p99 / p99.9 | 0.4 / 3.2 / 30.8 / 94.5 ms |
| max | 228 ms |

Single-process read throughput comfortably clears 1.5k req/s with a sub-ms
median; the tail grows past ~1k req/s as the GIL and per-request thread overhead
bite. This is the primary motivation for `SO_REUSEPORT` multi-process scaling.

### Async intake (`intake`) - 200 req/s, 15s

Mix: 25% `GET /health`, 75% `POST /v1/reviews?async=true` (enqueue + return).

| Metric | Value |
| --- | --- |
| Throughput | 200 req/s |
| Errors | 0 |
| p50 / p95 / p99 / p99.9 | 5.0 / 26.1 / 46.0 / 98.9 ms |
| max | 144 ms |

Enqueue latency is dominated by the synchronous SQLite write on the intake path;
Postgres with pooling and/or a Redis queue changes this profile.

## Reproduce

```bash
EVOAGENT_AUTH_REQUIRED=false EVOAGENT_PORT=8199 python -m evoagent &
python scripts/loadgen.py --base-url http://127.0.0.1:8199 \
    --scenario steady --duration 20 --rate 300 --warmup 2 --json steady.json
python scripts/loadgen.py --base-url http://127.0.0.1:8199 \
    --scenario intake --duration 15 --rate 200 --warmup 2 --json intake.json
```

For a full-stack run (Postgres + Redis) use `docker-compose.perf.yml` and the k6
scripts under [`perf/`](../perf); see [`performance.md`](performance.md).
