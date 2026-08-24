# ADR 0025: Prune operational history with state-aware anchors

- Status: accepted
- Date: 2026-08-17

## Context

Every task Trace event and every completed PR-session finding snapshot was kept
forever. That is useful for debugging and cross-push continuity, but makes
`trace_events` and `session_findings` grow with lifetime traffic. A database TTL
based only on `created_at` is unsafe: it can delete the final state used for
incident reconstruction, the latest completed session snapshot used by a new
turn, or the earlier snapshot an out-of-order pending turn must compare against.

Retention is also a governance decision. An application upgrade must not begin
deleting customer history merely because the schema contains new indexes or
markers.

## Decision

- Schema version 13 adds only retention indexes and nullable
  `tasks.trace_pruned_at` / `session_turns.findings_pruned_at` markers. The
  migration deletes no data.
- Retention remains disabled while `EVOAGENT_HISTORY_RETENTION_DAYS=0`.
  Operators explicitly set the age, interval, and batch size after approving
  backup, legal, and investigation requirements.
- The Store owns one atomic `prune_operational_history` operation so
  eligibility, markers, and deletion cannot diverge. PostgreSQL locks candidate
  events and coordinates all
  session start/completion/prune transactions with a per-session advisory lock.
- Trace pruning considers only terminal tasks and events older than the cutoff.
  It never removes the maximum/latest event for a task.
- Execution-artifact pruning considers only old `SUCCESS`/`CANCELLED` tasks
  without active admission. It removes the raw Diff payload, checkpoints and
  inter-agent messages in one transaction, records
  `execution_artifacts_pruned_at` in task input, and retains FAILED artifacts so
  operator resume remains possible.
- Primary Outbox intents are removed only for cancelled tasks or successful tasks
  whose external delivery is complete; active, failed and delivery-incomplete work is retained.
- Completed effect receipts are removed only after the configured retention age.
  In-progress receipts remain ownership fences; old comment markers and deterministic
  repair branches provide provider-side reconciliation after a completed receipt expires.
- Webhook delivery claims are removed after the same age only when that age is
  strictly longer than the configured replay window. A replay after retention is
  rejected as stale instead of being admitted as a new task.
- Old release observations are removed only when no rollout for their tenant and
  skill is running, preserving the rows used to deduplicate shadow retries.
- Session pruning considers only old completed turns that have a later completed
  turn. It refuses to remove a snapshot if a later pending turn has no completed
  intermediate predecessor, preserving the exact anchor needed when turns
  complete out of order. The latest completed turn is never pruned.
- The background coordinator caps rows/turns per transaction and ten batches per
  run, starts immediately when enabled, waits a configured interval, and joins
  service shutdown before Store closure. A session-turn batch is not an exact
  finding-row limit because one bounded input turn can contain several findings.
- Health exposes bounded status and exception type only. Fixed-cardinality
  counters, gauges, a Grafana panel, and a stalled-maintenance alert expose
  progress without tenant/repository/task labels.
- A rewritten completed turn clears its prune marker. Timeline readers expose
  `findings_retained`, and task records expose `trace_pruned_at`, so absence is
  not silently confused with an originally empty review.

## Consequences

The application can govern its dominant operational-history and raw execution
artifact tables without breaking live tasks, failed-task recovery, future PR
turns, out-of-order session completion, or active external-effect ownership.
Deletion is opt-in, transactional, observable, idempotent, and covered by the
PostgreSQL contract tests.

PostgreSQL operators must complete a v0.27-or-newer rollout before enabling the
job because older writers do not acquire the new session advisory lock. The
application does not replace backups, legal holds, `VACUUM`, native
partitioning, physical space reclamation, or a provider-managed lifecycle
service. Long-running production measurements are still required to choose a
safe period and batch size.
