# ADR 0023: Fixed-cardinality quality and economics telemetry

- Status: accepted
- Date: 2026-08-17

## Context

Availability and terminal success do not explain whether model cost is rising,
provider capacity is saturated, deterministic repairs are abstaining, or human
feedback is deteriorating. Adding tenant, repository, route, model, rule, or
free-form failure labels to Prometheus would create unbounded series, leak
cross-tenant operational activity, and make the monitoring system depend on
untrusted input. Treating sparse operator feedback as measured precision or
recall would be equally misleading.

## Decision

The process exports only fixed metric names for bounded states:

- known-billed model input/output tokens and micro-unit cost, with explicit
  active/shadow counters and latency histograms;
- concurrency, rate, or unknown capacity-rejection counters;
- repair attempts ending in published, deterministic abstention,
  verification-blocked, or failed;
- accepted, false-positive, missed-issue, or bad-fix feedback.

Model economics count a response when provider usage is known even if the
structured-output contract later rejects it. A transport failure with no usage
does not invent cost. Durable tenant/repository attribution remains in the
authorized usage and feedback stores, not metric labels.

Prometheus recording rules derive cost per terminal review, capacity-rejection
ratio, repair verification-block ratio, and a negative-feedback ratio. Alerts
use minimum-volume gates. The dashboard and runbook describe feedback as a
selective triage signal; only the independent annotation/evaluation pipeline may
support production quality claims.

## Consequences

- Metrics remain bounded across arbitrary tenants, repositories, routes, and
  source-controlled identifiers.
- Platform trends are immediately visible, while incident attribution requires
  tenant-authorized ledgers or failure records.
- Known billed failures are not hidden from economics, and unknown provider
  usage is not fabricated.
- Repair abstention is distinguishable from verification regression and
  infrastructure failure, so operators are not incentivized to weaken gates.
- Feedback alerts can trigger investigation but cannot self-modify prompts,
  thresholds, routing, or evaluation evidence.
