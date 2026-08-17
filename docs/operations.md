# Production operations and SLO runbook

This runbook turns EvoAgent's telemetry into operator actions. The source of
truth for objectives is [`ops/slo.toml`](../ops/slo.toml); Prometheus recording
and alert rules live in
[`ops/prometheus/evoagent.rules.yml`](../ops/prometheus/evoagent.rules.yml).

## SLO contract

| Objective | 30-day target | Indicator |
| --- | ---: | --- |
| API availability | 99.9% | Non-probe requests whose response is not 5xx |
| Async intake latency | 99% within 500 ms | Webhook and async-review intake histogram |
| Review success | 99% | Successful / all terminal review executions |

`429` is deliberate client-level throttling and does not consume the 5xx
availability budget. A heavy-gate `503` does consume it: admitted capacity is
part of the service contract. Authentication/validation 4xx responses do not
consume availability but must be monitored for client regressions separately.

Evaluate the current Prometheus window in CI, a release gate, or an incident:

```bash
evoagent-slo --catalog ops/slo.toml \
  --prometheus-url https://prometheus.internal.example \
  --allowed-host prometheus.internal.example
```

The command exits `0` when all objectives pass, `1` on an SLO breach, and `2`
when an objective has insufficient samples. `--allow-no-data` is appropriate
only for a new, intentionally idle environment. Credentials come from
`EVOAGENT_PROMETHEUS_BEARER_TOKEN`; redirects and environment HTTP proxies are
disabled and non-loopback endpoints require HTTPS.

Run one `EVOAGENT_WEB_WORKERS=1` process per production pod and scale pods
horizontally. The built-in metric registry is process-local, so scraping one
shared `SO_REUSEPORT` socket cannot aggregate several local worker processes.
Prometheus should attach deployment/cluster/region labels at scrape time and
aggregate across pods in queries.

## Availability fast burn

This alert means the 99.9% monthly error budget is being consumed at more than
14.4× over both 5-minute and 1-hour windows.

1. Check `/ready` and split 5xx rate by pod, deployment version, and region.
2. Stop rollout or route traffic back to the last healthy version if failures
   correlate with a release.
3. Check Store/Redis readiness, circuit-breaker state, pool availability,
   Outbox age, and worker liveness before restarting anything.
4. Preserve failed task IDs and traces; do not replay DLQ/Outbox items until the
   dependency or code fault is fixed.
5. Record incident start/end, affected requests, budget consumed, and rollback
   decision.

## Availability slow burn

This is a sustained 6× budget burn over 30-minute and 6-hour windows. Open an
incident ticket, freeze nonessential releases, compare error types and tenant
distribution, and schedule remediation before the remaining budget reaches
zero. Page immediately if the fast-burn alert also fires.

## Intake latency

1. Compare intake RPS, p99, `outbox_oldest_age_seconds`, Postgres pool available,
   and CPU saturation.
2. If DB pool waiters rise, confirm total pool demand across pods is below the
   Postgres connection budget before increasing a pool.
3. If only SQLite is configured, migrate production traffic to PostgreSQL;
   SQLite intake is a development topology.
4. Shed burst traffic with the existing token bucket instead of increasing
   unbounded HTTP concurrency.

## Queue or Outbox stale

- **Outbox stale, queue healthy:** inspect dispatcher error, Store connectivity,
  and `outbox_publish_failures_total`. A committed task is safe in the Store;
  restart the dispatcher only after confirming another owner is not publishing.
- **Queue stale:** inspect Redis health, worker count, task duration, and lease
  reclaim. Scale workers only if downstream GitHub/model/DB capacity can absorb
  them.
- Never edit queue/Outbox rows manually. Use `/v1/outbox/replay` or
  `/v1/queue/dead-letters/replay`, which audit the operation and preserve
  idempotency keys.

## Dead letters

Classify the error as permanent input/policy failure or transient infrastructure
failure. Fix the root cause, replay one item, verify its terminal state and
external effects, then replay the bounded batch. A growing DLQ is not resolved
by deleting entries; export task IDs and retain the audit trail.

## Review failures

Split failures by reviewer, model route, repository, and error type using task
traces and the metadata-only model ledger. Check policy rejections separately
from execution failures. Roll back a candidate Prompt/Skill if the failure spike
is lane-correlated; do not weaken evidence or repair gates to restore throughput.

## Plugin runtime

`plugin_runtime_ready=0` means the capability graph is stopping or failed.
Inspect startup dependency/cycle errors, the active Profile, and the trusted
plugin allowlist. Roll back the plugin package or Profile as one deployment;
live global hot-swap is intentionally unsupported.

## Proof Runner

Use `proof_inconclusive_total / proof_runs_total` and runner capacity logs to
distinguish bad reproductions from infrastructure uncertainty. Runner errors
must never be reclassified as failed reproductions. Follow
[`remote-proof-runner.md`](remote-proof-runner.md) for isolation, key rotation,
artifact retention, and replica limits.

## Release checklist

1. Ruff, mypy, unit/contract tests, dependency audit, wheel build pass.
2. PostgreSQL/Redis/container boundary CI passes without skipped external tests.
3. Performance regression gates and SLO catalog validation pass.
4. Schema migration job completes before new application pods become ready.
5. Canary health and error budget are observed before widening traffic.
6. Rollback artifact, previous image digest, and migration compatibility are
   recorded.
7. `evoagent-dr` restored into a disposable database, content fingerprints
   matched, cleanup succeeded, and the report is within declared RPO/RTO.

The complete procedure and evidence contract are in the
[disaster-recovery runbook](disaster-recovery.md). Database recoverability is
automated; Redis rehydration, regional failover, and a production-shaped soak
remain required before claiming full service disaster-recovery readiness.
