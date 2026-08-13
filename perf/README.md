# Performance test kit

Two interchangeable load tools plus a full-stack rig. Read
[`../docs/performance.md`](../docs/performance.md) for SLOs and methodology and
[`../docs/performance-baseline.md`](../docs/performance-baseline.md) for captured
numbers.

## 0. One-command baseline capture

[`../scripts/perf_baseline.sh`](../scripts/perf_baseline.sh) orchestrates the
whole suite (read knee sweep, intake, spike, soak, overload/backpressure,
multi-worker scaling, micro-benchmarks), boots/tears down servers per scenario,
and writes per-run JSON + CSV to `perf/baseline-<timestamp>/`.

```bash
scripts/perf_baseline.sh all           # everything
scripts/perf_baseline.sh overload      # just the 429/503 + Retry-After proofs
scripts/perf_baseline.sh single --quick # fast self-test of the harness
```

## 1. Pure-Python generator (no external tools)

`scripts/loadgen.py` is an open-model, constant-arrival-rate generator (stdlib
only) that is coordinated-omission-aware and exits non-zero on threshold breach -
so it works offline and in CI.

```bash
python -m evoagent &   # or docker compose -f docker-compose.perf.yml up
python scripts/loadgen.py --base-url http://127.0.0.1:8080 \
    --scenario steady --duration 60 --rate 500 --p99-ms 150 --max-error-rate 0.001
```

Scenarios: `smoke`, `steady`, `intake`, `stress-ramp`, `spike`, `soak`.

## 2. k6 (for teams that have it)

```bash
k6 run perf/k6/steady.js                 # average load
k6 run -e PEAK=3000 perf/k6/stress.js    # find the knee
k6 run perf/k6/spike.js                  # burst + recovery
k6 run -e DURATION=2h perf/k6/soak.js    # endurance
```

Every script uses k6's **arrival-rate** executors (open model) and overridable
`-e BASE_URL=... -e RATE=...` env vars.

## 3. Full-stack rig (Postgres pool + Redis queue + multi-worker)

```bash
docker compose -f docker-compose.perf.yml up --build   # app on :8080
# run the generator or k6 from the host, or the bundled k6 runner:
docker compose -f docker-compose.perf.yml --profile k6 run --rm k6 run /perf/steady.js
```

> The perf rig disables auth for convenience - never use it as a deployment
> template. Tune workers/pool via `EVOAGENT_WEB_WORKERS`, `EVOAGENT_ASYNC_WORKERS`,
> `EVOAGENT_PG_POOL_MAX`, and admission control via `EVOAGENT_RATE_LIMIT_RPS` /
> `EVOAGENT_MAX_INFLIGHT_HEAVY`.
