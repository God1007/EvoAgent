# Performance Baseline (captured)

A real, reproducible baseline captured with the in-repo tooling
([`scripts/loadgen.py`](../scripts/loadgen.py) and
[`scripts/microbench.py`](../scripts/microbench.py)), orchestrated end-to-end by
[`scripts/perf_baseline.sh`](../scripts/perf_baseline.sh). Re-capture on your own
hardware before trusting these numbers - they exist to catch **regressions**, not
to advertise absolute peak throughput.

Reproduce everything below with:

```bash
scripts/perf_baseline.sh all           # full suite -> perf/baseline-<timestamp>/
scripts/perf_baseline.sh single        # only the read/intake/spike/soak suite
scripts/perf_baseline.sh overload      # only the backpressure suite
scripts/perf_baseline.sh multiworker   # only the SO_REUSEPORT scaling suite
scripts/perf_baseline.sh micro         # only the hot-path micro-benchmarks
```

## Environment

| Field | Value |
| --- | --- |
| Host | Apple Silicon (arm64), 18 logical cores |
| OS | macOS 26.4.1 |
| Python | 3.12.13 |
| Server | single process (`EVOAGENT_WEB_WORKERS=1`), `ThreadingHTTPServer` |
| Store | SQLite (temp db) |
| Queue | `memory-ephemeral` |
| Auth | disabled |
| Load generator | co-located on the same host (indicative, not isolated) |

> Co-locating the generator with the server understates achievable throughput
> and inflates the tail; treat these as a conservative floor. The full-stack
> profile (Postgres pool + Redis queue + multi-worker) was **not** captured in
> this environment - see [§7](#7-full-stack-profile-not-captured-here).

## 1. Read mix - breakpoint / knee sweep

`steady` mix (3:1:1 `GET /health` : `/ready` : `/metrics`), 20 s per rate,
32 connections, single process. The knee is defined as the first rate where p99
breaches the 150 ms read SLO or errors appear.

| Target rate | Throughput | p50 | p95 | p99 | p99.9 | max | errors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 300 | 300 | 0.7 | 1.1 | 1.5 | 6.4 | 13.4 | 0 |
| 1000 | 1000 | 0.6 | 0.9 | 2.3 | 35.2 | 98.0 | 0 |
| 1500 | 1500 | 0.4 | 0.6 | 2.9 | 102.7 | 173.4 | 0 |
| 2000 | 2000 | 0.3 | 0.4 | 1.1 | 93.7 | 161.7 | 0 |
| 3000 | 3000 | 0.2 | 0.3 | 3.6 | 144.9 | 217.9 | 0 |
| 4000 | 4000 | 0.2 | 0.3 | 6.7 | 169.8 | 286.0 | 0 |

*(latency in ms)*

**Knee: not reached at 4000 req/s.** Single-process read p99 stays under 7 ms
through 4000 req/s with zero errors; it is the **p99.9 tail** (35 -> 170 ms) that
grows past ~1k req/s as the GIL and per-request thread overhead bite. The true
knee for the read mix is beyond 4000 req/s on this hardware, and the co-located
generator is itself a limiting factor here. This is the primary motivation for
`SO_REUSEPORT` multi-process scaling (see [§6](#6-multi-worker-scaling-so_reuseport)).

## 2. Async intake

Mix: 25% `GET /health`, 75% `POST /v1/reviews?async=true` (enqueue + return).
200 req/s, 20 s.

| Metric | Value |
| --- | --- |
| Throughput | 200 req/s (no shedding) |
| Errors | 0 |
| p50 / p95 / p99 / p99.9 | 10.9 / 55.2 / 150.3 / 275.3 ms |
| max | 385 ms |

Enqueue latency is dominated by the synchronous SQLite write on the intake path;
Postgres with pooling and/or a Redis queue changes this profile.

## 3. Spike (burst + recovery)

`spike` scenario: first 20% of the window at 10% rate, then an instant jump to
1500 req/s. 20 s, read mix.

| Metric | Value |
| --- | --- |
| Throughput | 1500 req/s |
| Errors | 0 |
| p50 / p95 / p99 / p99.9 | 0.4 / 518 / 531 / 611 ms |
| max | 2020 ms |

The instantaneous 0 -> 1500 req/s jump produces a one-off latency spike (p95/p99
around 0.5 s, worst request ~2 s) while the thread pool and connection backlog
absorb the burst, then the steady-state median returns to sub-millisecond. No
requests are dropped and no errors occur - the burst is absorbed, not shed.

## 4. Soak / endurance

`soak` scenario: 180 s @ 500 req/s, read mix. RSS sampled before/after; queue
and rejection gauges read from `/metrics` afterward.

| Metric | Value |
| --- | --- |
| Throughput | 500 req/s (sustained) |
| Errors | 0 |
| p50 / p95 / p99 / p99.9 | 0.5 / 3.1 / 5.5 / 10.7 ms |
| RSS before -> after | 123.0 MB -> 137.4 MB (**+14.4 MB**) |
| Queue depth (after) | 0 (drained) |
| Dead letters | 0 |

Latency is flat and low across the window with no errors, and the queue fully
drains. RSS grew ~14 MB over 3 minutes. This capture predates the opt-in
state-aware retention of `trace_events` / superseded `session_findings` and ran
with retention disabled, so it cannot attribute the increase to a specific
surface. In-memory metric histograms and retained operational rows should be
measured separately in a multi-hour run. Native TTL/partitioning and a true
durable Postgres + Redis soak remain outstanding.

## 5. Overload / backpressure

Proves the service **sheds** load instead of collapsing. This baseline does not
configure trusted proxy CIDRs, so the limiter uses the localhost socket peer and
all requests share one bucket. Probe paths
(`/health`, `/ready`, `/metrics`) bypass the limiter by design.

### 5a. Rate-limit shedding (`429` + `Retry-After`)

Server: `EVOAGENT_RATE_LIMIT_RPS=100`, `EVOAGENT_RATE_LIMIT_BURST=20`. Drove the
intake mix at **800 req/s** for 15 s (12 000 requests: 3000 probe + 9000
rate-limited `POST`).

| Outcome | Count |
| --- | --- |
| `200` health probes (bypass limiter) | 3000 |
| `202` accepted (within ceiling) | 1519 |
| `429` shed | 7481 |
| `Retry-After` on shed responses | `1` (present) |

~83% of non-probe traffic was cleanly rejected with a `Retry-After` header;
accepted volume (~1500 over 15 s) matches the 100 req/s ceiling. No 5xx, no
connection resets.

### 5b. Heavy-gate shedding (`503` + `Retry-After`)

Server: `EVOAGENT_MAX_INFLIGHT_HEAVY=4`, no rate limit. Saturated the
bounded-concurrency gate with **synchronous** `POST /v1/reviews` at concurrency
24 (96 requests; each runs the full multi-agent graph, so requests stay in-flight
long enough to overflow the gate).

| Outcome | Count |
| --- | --- |
| `201` processed | 4 |
| `503` shed | 92 |
| `Retry-After` on shed responses | `1` (present) |

The gate admits exactly its limit and sheds the rest immediately (non-blocking
`try_acquire`) with `Retry-After`, rather than queueing work inside the server.

### 5c. Recovery

Dropping back to **40 req/s** (under the 100 ceiling) after the overload: 600
requests, **0 shed** (`200`:150, `202`:450). The limiter self-recovers as soon
as arrival falls below the ceiling.

## 6. Multi-worker scaling (`SO_REUSEPORT`)

Read mix @ 3000 req/s, 64 connections, varying `EVOAGENT_WEB_WORKERS`.

| Workers | Throughput | p99 | p99.9 | max | errors |
| --- | --- | --- | --- | --- | --- |
| 1 | 3000 | 2.0 | 129.2 | 588 | 0 |
| 2 | 3000 | 2.4 | 150.9 | 2031 | 0 |
| 4 | 3000 | 2.4 | 130.4 | 2040 | 0 |

*(latency in ms)*

**No scaling signal on macOS - expected.** `SO_REUSEPORT` connection
load-balancing across worker processes is a Linux 3.9+ behavior; macOS/BSD
semantics differ (the last binder tends to receive connections), so multi-worker
on macOS is **dev-only** and shows no throughput improvement. A single process
already absorbs 3000 req/s here, so this run only confirms the workers boot,
bind the shared port, and stay correct - it does **not** measure horizontal
scaling. Capture the scaling curve on Linux to characterize it properly.

## 7. Full-stack profile (not captured here)

The production-shaped profile - **Postgres connection pool + Redis Streams queue
+ multiple web workers** via [`docker-compose.perf.yml`](../docker-compose.perf.yml)
- could not be captured in this environment because Docker (and local
Postgres/Redis) were unavailable. This changes the intake and soak profiles most
(the SQLite synchronous write on the enqueue path is replaced by pooled Postgres
+ durable Redis delivery).

To capture it on a Docker-capable Linux host:

```bash
docker compose -f docker-compose.perf.yml up --build -d
# then, from the host:
python scripts/loadgen.py --base-url http://127.0.0.1:8080 \
    --scenario steady --duration 60 --rate 3000 --p99-ms 150 --json steady.json
python scripts/loadgen.py --base-url http://127.0.0.1:8080 \
    --scenario intake --duration 60 --rate 1000 --p99-ms 500 --json intake.json
# or the bundled k6 runner (open-model executors):
docker compose -f docker-compose.perf.yml --profile k6 run --rm k6 run /perf/stress.js
```

## 8. Hot-path micro-benchmarks

`scripts/microbench.py`, ns/op (best of 5 repeats), against built-in regression
budgets. All within budget.

| Benchmark | ns/op | ops/sec | budget (ns) |
| --- | --- | --- | --- |
| `parse_unified_diff` | 111 244 | 8 989 | 200 000 |
| `finding_fingerprint` | 2 183 | 458 096 | 20 000 |
| `scoped_fingerprint` | 2 217 | 451 159 | 20 000 |
| `classify_findings` | 156 790 | 6 378 | 400 000 |
| `codegraph_impact_of` | 56 432 | 17 721 | 2 000 000 |

## Summary and follow-ups

- **Reads**: single process sustains >= 4000 req/s with p99 < 7 ms and zero
  errors; the tail (p99.9) is the first thing to move under load.
- **Intake**: 200 req/s clean; bounded by the synchronous SQLite enqueue write.
- **Spike**: 1500 req/s instantaneous burst absorbed with no errors (transient
  ~0.5 s tail during the jump).
- **Soak**: flat latency and a drained queue, but ~14 MB / 3 min RSS growth to
  remeasure with v0.27 retention enabled and per-surface database/RSS evidence.
- **Overload**: rate-limit (`429`) and heavy-gate (`503`) both shed cleanly with
  `Retry-After` and self-recover.
- **Micro**: all hot paths within regression budgets.

Outstanding captures (need a Docker/Linux host): full-stack Postgres+Redis
profile, Linux `SO_REUSEPORT` scaling curve, and a multi-hour durable-backend
soak.
