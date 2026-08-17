# ADR 0026: Coordinate durable tenant review admission in the Store

- Status: accepted
- Date: 2026-08-17

## Context

Process-local rate limits and heavy-request semaphores protect one HTTP worker,
but they do not bound how much durable review work a tenant can commit across
replicas. Counting task state is also incorrect: an async delivery marks its
task `FAILED` before Redis retry or DLQ disposition, while a synchronous failure
with the same state is terminal. A noisy tenant could therefore grow Store,
Outbox, queue, model, and GitHub pressure without a durable ownership signal.

Resume creates another race. If it directly submits to the queue, a crash can
separate task state from delivery intent. Even after an atomic resubmission, a
late DLQ callback from the previous delivery must not release the new capacity
claim.

## Decision

- Schema version 14 adds one `task_admissions` row per public review task. It
  records tenant, active ownership, synchronous/async failure semantics, a
  monotonically increasing generation, acquisition/release timestamps, and a
  bounded release reason.
- `EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS` is a uniform per-tenant hard limit. Zero
  disables rejection but still tracks slots, enabling an observable rollout.
- SQLite uses `BEGIN IMMEDIATE`. PostgreSQL always takes a transaction advisory
  lock keyed by the stable tenant identity, including while the limit is zero,
  so all v0.28 writers participate in the same coordination protocol. Capacity
  count, rejection audit, task/session/Diff, admission, and Outbox changes share
  the intake transaction.
- Async execution failure retains the slot because delivery still owns retry.
  Success, cancellation, synchronous failure, or final dead-letter disposition
  releases it. Offline queue reconstruction includes a `FAILED` task only when
  it still has an active admission.
- Resume locks the task, checks tenant capacity, increments the generation, and
  commits a unique Outbox message in one transaction. Queue/DLQ payloads carry
  that generation; final delivery release succeeds only for the currently
  active generation. Repeated resume while active is a no-op.
- Capacity rejection returns `429` and a bounded `Retry-After`, emits fixed-
  cardinality metrics, and records a tenant-authorized audit event. Webhook
  delivery identity remains claimed without creating a partial task/session so
  GitHub can retry the same delivery after capacity becomes available.
- `/health` exposes only limit configuration. The administrator capacity view
  is tenant-scoped; Prometheus exports global/fixed-cardinality gauges and
  counters without tenant labels.

## Consequences

One tenant cannot commit more than its configured outstanding-review allowance
across database-sharing replicas. Retry, crash reconstruction, dead-letter
handling, and resume now share an explicit durable lifecycle instead of
inferring ownership from task state. Resume also closes the direct-submit crash
window through the transactional Outbox.

The cap is deliberately conservative: a dead Outbox or retry-managed failure
keeps its slot until audited replay, cancellation, or final DLQ handling. This
prevents silent over-admission but requires operator remediation for stuck work.
The control limits durable occupancy; it does not implement weighted-fair Redis
dequeue order or tenant-specific worker shares.

Migration backfills tasks whose stored state is visibly non-terminal, but
cannot infer a pre-v0.28 Redis retry whose task is already `FAILED`. Operators
must apply migration 14, complete an all-writer v0.28 rollout, drain or resolve
that legacy retry backlog, validate observed occupancy, and only then enable a
non-zero limit. Old writers cannot safely overlap enforcement because they do
not take the tenant lock or emit delivery generations.
