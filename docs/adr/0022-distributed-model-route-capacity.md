# ADR 0022: Database-coordinated model-route capacity

- Status: accepted
- Date: 2026-08-17

## Context

Weighted routing expresses intended traffic share but cannot enforce a provider
concurrency or requests-per-minute contract. Per-process semaphores multiply the
allowed capacity when workers scale horizontally. Keying limits by topology
would also double them while old and new releases overlap. Calling a saturated
provider and relying on HTTP 429 wastes latency, provider quota, and usage-ledger
work.

## Decision

Topology v2 routes may declare `capacity_max_inflight` and
`capacity_requests_per_minute`. Before reserving usage or calling a provider, the
gateway asks `ModelUsageStorePort` for one atomic admission. A rejection is an
eligible bounded-fallback failure and does not create a usage row.

PostgreSQL serializes decisions with a transaction advisory lock keyed by stable
route ID. Expiring durable leases enforce max in-flight, while route/minute rows
enforce a fixed UTC-minute rate ceiling. The pool spans topology hashes so a
rolling deployment shares one allowance; topology remains stored as evidence.
SQLite uses `BEGIN IMMEDIATE` for equivalent development behavior. Lease TTL
must exceed provider timeout. Crash expiry releases concurrency conservatively;
rate admissions are not refunded.

The administrator capacity report exposes current in-flight count, minute
admissions/rejections, earliest lease expiry, breaker state, and availability.
When every active route in a priority tier declares comparable capacity, it
normalizes the declared values into a read-only weight recommendation. Runtime
state is never mutated: weights change only through reviewed topology and
deployment.

The tenant-scoped API redacts exact global counters for routes shared by more
than one tenant (or with no explicit tenant binding), while still returning the
availability decision. Exact counters are visible only for a route explicitly
bound to that one tenant; platform operators use restricted storage/telemetry
for shared-pool investigation.

## Consequences

- Replicas and overlapping releases cannot independently oversubscribe a stable
  provider route.
- A stable route ID becomes the capacity-pool identity and must not be reused for
  an unrelated endpoint or quota pool.
- Admission happens before budget reservation. If budget subsequently rejects,
  the consumed minute unit is not refunded; this deliberately favors the hard
  provider ceiling over utilization.
- Fixed windows may reject near a minute boundary more abruptly than a token
  bucket, but are deterministic, durable, and easy to audit across adapters.
- Recommendations represent declared capacity, not a forecast, and require
  operator review before GitOps activation.
- Shared-pool protection is global, but its exact counters are not a
  cross-tenant reporting channel.
