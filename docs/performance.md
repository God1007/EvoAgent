# EvoAgent Performance and Load Testing

This document defines how EvoAgent behaves under load, how we measure it, and how
to reproduce the benchmarks. It is the companion to the load-test kit under
[`scripts/loadgen.py`](../scripts/loadgen.py), the k6 scripts under
[`perf/`](../perf), and the captured baseline in
[`performance-baseline.md`](performance-baseline.md).

The guiding principle: **a benchmark number is only meaningful with a stated
workload, a stated environment, and percentiles (not averages).**

## 1. Service level objectives

The table below is the per-instance engineering performance target. The formal
30-day production contract (99.9% non-probe availability, 99% async intake
within 500 ms, and 99% terminal review success) is machine-readable in
[`ops/slo.toml`](../ops/slo.toml), evaluated by `evoagent-slo`, and paired with
multi-window burn alerts in the [operations runbook](operations.md).

Targets are per API instance on a modern 4-core host. They are goals for the
load-test gate, not guarantees; recalibrate against your hardware and record the
result in [`performance-baseline.md`](performance-baseline.md).

| Class | Endpoints | Throughput (per instance) | Latency SLO |
| --- | --- | --- | --- |
| Read | `/health`, `/ready`, `/metrics`, `/v1/sessions/*`, `/v1/tasks/{id}` | >= 2000 RPS | p99 < 50 ms |
| Async intake | `/v1/reviews?async=true`, `/webhooks/github` | >= 500 RPS | p99 < 150 ms |
| CPU-heavy | `/v1/reviews` (sync), `/v1/codegraph/impact` | workload-bound | p95 within task budget |
| Sandboxed | `/v1/proofs` | isolation-bound | best-effort |
| Reliability | all | - | error rate < 0.1% |

End-to-end (enqueue -> task terminal state) is bounded by the reviewer and the
worker pool, not the HTTP layer; it is tracked separately from request latency.

## 2. Workload model

Real traffic is a mix, not a single endpoint. The load generator models these
scenarios (see `SCENARIOS` in [`scripts/loadgen.py`](../scripts/loadgen.py)):

- **Read-heavy**: health/readiness, session timelines, task status. Dominant in
  dashboards and polling clients.
- **Async intake**: `POST /v1/reviews?async=true` and GitHub webhooks. Must return
  quickly and shed load rather than block.
- **Webhook `synchronize` bursts**: many pushes to the same PR in a short window.
  Stresses session get-or-create, the `UNIQUE(session_id, sequence)` constraint,
  and the Postgres advisory lock.
- **CPU-heavy**: `/v1/codegraph/impact` on large source sets.
- **Sandboxed**: `/v1/proofs` (container-isolated; excluded from the default
  latency SLO, measured for saturation and correctness only).

## 3. Test types

Run all of these; do not report only "average load".

- **Smoke** (1-5 VU): validates the script and wiring.
- **Average-load**: steady expected concurrency at a fixed arrival rate.
- **Stress / breakpoint**: ramp arrival rate until the "knee" - the RPS beyond
  which p99 or error rate breaches SLO. Report the knee, not just a passing run.
- **Spike**: 0 -> peak instantly (models webhook bursts); measure recovery.
- **Soak / endurance** (hours): watch for memory growth, connection/thread leaks,
  queue backlog, and dead-letter growth.
- **Overload / backpressure**: exceed capacity on purpose and assert graceful
  degradation - `429`/`503` with `Retry-After`, DLQ intact, self-recovery.

## 4. Methodology (how to avoid lying to yourself)

- **Percentiles, not averages.** Report p50/p95/p99/p99.9. A good mean can hide a
  terrible tail.
- **Open model / constant arrival rate.** Fire requests on a schedule independent
  of responses to avoid *coordinated omission* (the classic way closed-loop tools
  hide latency during stalls). `scripts/loadgen.py` records latency against the
  *scheduled* send time and reports scheduling delay separately.
- **Warm up** before measuring; discard the warmup window.
- **Isolate** the load generator from the system under test (separate host/core
  set) for real runs; co-located runs are only indicative.
- **Monitor saturation** throughout: CPU, memory, thread count, queue depth, DB
  pool in-use/idle, and rejected counters, all exposed on `/metrics`.
- **Repeat** and take the median of several runs.

## 5. Running the benchmarks

Pure-Python generator (no external tools; works offline and in CI):

```bash
python -m evoagent &                 # or: evoagent
EVOAGENT_AUTH_REQUIRED=false \
python scripts/loadgen.py --base-url http://127.0.0.1:8080 \
    --scenario steady --duration 60 --rate 500 \
    --p99-ms 150 --max-error-rate 0.001 --json out.json
```

`loadgen.py` exits non-zero if the p99 or error-rate thresholds are breached, so
it doubles as a CI perf-regression gate (`.github/workflows/perf.yml`).

k6 (for teams that have it; full-stack via `docker-compose.perf.yml`):

```bash
docker compose -f docker-compose.perf.yml up -d
k6 run perf/k6/steady.js
```

Micro-benchmarks for hot code paths (regression thresholds on pure functions):

```bash
python scripts/microbench.py --json micro.json
```

## 6. Capacity and scaling model

- **HTTP layer**: the stdlib router scales across cores via multiple worker
  processes bound with `SO_REUSEPORT` (`EVOAGENT_WEB_WORKERS`). Workers are
  stateless; scale horizontally behind a reverse proxy/LB. A master supervisor
  restarts crashed workers (with backoff and a restart-storm cap), and on
  `SIGTERM` it forwards the signal, waits a bounded grace period
  (`EVOAGENT_SHUTDOWN_GRACE_SECONDS`), then `SIGKILL`s any straggler so
  termination never hangs. Notes: kernel connection load-balancing across
  `SO_REUSEPORT` sockets is a Linux 3.9+ behavior (macOS/BSD semantics differ,
  so multi-worker there is best treated as dev-only); and any same-UID local
  process can bind the shared port, so rely on network isolation in production.
  Graceful drain flips `/ready` to 503 (LB stops routing) and lets in-flight
  HTTP requests finish; in-flight async queue work is only recovered with the
  durable Redis backend (the in-memory queue loses it on exit).
- **Backpressure**: a per-client token-bucket rate limiter and a
  bounded-concurrency gate for heavy endpoints shed load with `429`/`503` +
  `Retry-After` instead of collapsing. The client key is the socket peer unless
  that peer matches `EVOAGENT_TRUSTED_PROXY_CIDRS`; only then is the bounded
  `X-Forwarded-For` chain consumed from right to left. Invalid or attacker-added
  prefixes fail closed to a previously verified hop rather than creating a new
  bucket.
- **Resilience**: outbound GitHub and LLM calls are wrapped in a circuit breaker
  with exponential backoff + jitter, so an upstream outage fails fast instead of
  exhausting workers.
- **Storage**: Postgres uses a real connection pool (`psycopg_pool` is a core
  runtime dependency). Size the pool with `EVOAGENT_PG_POOL_MAX`. Note the
  total backend budget is roughly `web_workers * pg_pool_max` (each worker
  process owns its own pool), so keep it under the server's `max_connections`.
  SQLite is single-writer and intended for single-node/dev.
- **Queue**: use Redis Streams (`EVOAGENT_REDIS_URL`) in production for durable,
  crash-safe delivery; the in-process queue is non-durable.
- **Metrics topology**: the built-in registry is process-local. Run one web
  worker per production pod and scale pods horizontally so each Prometheus
  scrape is complete; `SO_REUSEPORT` multi-worker mode is a load-test/dev option,
  not an in-process metrics aggregation mechanism.

## 7. Known follow-ups (not yet implemented)

- Full ASGI/FastAPI rewrite (kept stdlib + multi-process by design for now).
- PgBouncer, read replicas, and native partitioning/managed TTL. The application
  now offers opt-in, invariant-aware bounded retention for `trace_events` and
  superseded `session_findings`, but a multi-hour PostgreSQL soak and physical
  table-reclamation policy remain deployment work.
- microVM isolation (Firecracker/Kata) as an alternative `proof.executor`
  provider for hostile public multi-tenancy.
- A dedicated concurrency guard for `/webhooks/github` `synchronize` fan-out.
