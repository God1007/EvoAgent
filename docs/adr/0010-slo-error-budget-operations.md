# ADR 0010: Versioned SLOs and error-budget operations

- Status: accepted
- Date: 2026-08-17

## Context

Latency benchmarks and a `/metrics` endpoint do not form an operable service
contract. The previous metrics lacked response totals by outcome, queue length
did not reveal stale work, review duration was a summary that could not produce
tail percentiles, and no versioned objective connected telemetry to paging or
release decisions.

## Decision

Define a versioned 30-day SLO catalog with three production objectives:

- 99.9% non-probe HTTP availability;
- 99% of asynchronous review/webhook intake within 500 ms;
- 99% successful terminal review execution.

HTTP telemetry uses fixed request classes (`probe`, `read`, `intake`, `heavy`,
`proof`, `write`) and status families, avoiding repository/tenant/path labels
with unbounded cardinality. Probe traffic is excluded from availability so
health checks cannot hide user-visible failure. `429` remains a deliberate 4xx
throttle; capacity `503` consumes availability budget. Review and Proof latency
are histograms. Queue/Outbox oldest age and current DLQ depth detect stuck work
that a length-only dashboard misses.

Retryable worker failures increment `review_attempts_failed_total`; they enter the
terminal review-failure series and canary result only once, when queue exhaustion
releases the task admission into the DLQ.

Ship the catalog, Prometheus recording/multi-window burn alerts, Grafana
dashboard, and operator runbook as versioned package assets. `evoagent-slo`
queries one scalar per indicator/sample count using an exact-host, HTTPS,
no-redirect, no-environment-proxy client. Insufficient samples return `no-data`
and never claim success.

The evaluator also requires a currently healthy target in the `evoagent` scrape
job. If all targets are down or the job is missing, every objective returns
`no-data`, preserving its observed sample count but withholding achievement and
error-budget claims. Historical samples alone cannot prove that current
collection is functioning. This guard does not prove complete replica discovery
or continuous coverage over the entire evaluation window.

## Consequences

- SLO definitions and alert thresholds change through code review with the
  application version rather than dashboard-only edits.
- The 500 ms production intake threshold is intentionally looser than the
  150 ms per-instance engineering target; the former includes deployment and
  dependency variation while the latter catches code regressions.
- The metric registry is process-local. Production runs one process per pod and
  scales pods horizontally, so every Prometheus target is complete.
- SLO compliance still requires adequate Prometheus retention and independent
  production traffic. Synthetic load tests remain regression evidence, not an
  availability claim.
- Backup/restore, RPO/RTO, regional failover, and long-duration soak evidence
  remain separate Phase 5 deliverables.
