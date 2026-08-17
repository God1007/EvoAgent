# ADR 0016: Reconcile crashed model reservations conservatively

- Status: Accepted
- Date: 2026-08-17

## Context

The model gateway reserves worst-case Token and cost capacity before network
I/O, then replaces it with actual usage. A process can crash after the provider
accepts a request but before the ledger completes. Automatically setting that
row to zero would restore quota availability but could let the tenant exceed a
hard cost budget. Keeping it as `reserved` forever obscures whether work is
active and gives operators no safe correction path.

Provider APIs do not share one portable idempotency or usage-query protocol, so
the application cannot infer whether an interrupted request was billed.

## Decision

- Configure a reservation TTL that must exceed the provider request timeout.
  Startup and rate-limited runtime maintenance scan a bounded indexed batch of
  older `reserved` rows.
- Expired rows atomically become `uncertain`. Budget queries count both
  `reserved` and `uncertain` at their worst-case reserved amounts.
- A late response carrying real usage may still complete an `uncertain` row.
- A manage-authorized operator may reconcile an `uncertain` row only inside the
  current tenant and only with explicit non-negative input, output, and cost
  values obtained from provider evidence.
- Reconciliation and its actor/resource/value audit record commit in the same
  Store transaction. Failure of either write rolls back both.
- The transition is state-guarded, so repeated or concurrent operator requests
  cannot overwrite an already settled row.
- SQLite serializes the bounded scan with `BEGIN IMMEDIATE`; PostgreSQL uses a
  `FOR UPDATE SKIP LOCKED` candidate CTE for safe multi-replica maintenance.

## Consequences

Operators can distinguish live calls from billing uncertainty and repair quota
accuracy without weakening the default cost ceiling. A missing provider record
can release the reservation only through an explicit, attributable zero-usage
reconciliation—not merely because time passed.

This does not provide automatic provider-led reconciliation. Adapters for a
specific provider may later fetch signed usage exports, but they must feed the
same tenant/state/atomic-audit boundary. Candidate route promotion and weighted
load balancing remain separate decisions.
