# ADR 0006: Explicit use-case boundaries and atomic webhook intake

- Status: accepted
- Date: 2026-08-17

## Context

`ReviewService` originally owned runtime composition, health, review admission,
queue execution, GitHub webhook parsing, session continuity, proof execution,
repair publication, feedback, and repository-policy management. That made the
class a convenient API facade but an unsafe change boundary: a unit test for one
business operation needed most infrastructure, and webhook intake committed its
delivery record before it created a session, task, and outbox message.

If the process stopped in that interval, replaying the same GitHub delivery was
treated as a duplicate even though no task existed. The delivery could remain
permanently accepted but unprocessable.

## Decision

Keep `ReviewService` as the backward-compatible public facade and composition
root, but move business orchestration into `evoagent.application`:

- `ReviewUseCases` owns review admission, execution, async delivery, feedback,
  resume/cancel, and dead-letter transitions;
- `WebhookUseCases` validates and routes GitHub PR deliveries;
- `SessionUseCases` owns continuity and code-impact operations;
- `RepairUseCases` owns proof execution and idempotent deterministic repair;
- `PolicyUseCases` owns audited repository-policy reads and writes;
- the existing `EvolutionEngine` remains the independently composed evolution
  use-case capability.

Each application object receives focused Protocols, immutable option objects,
and narrow callbacks. Infrastructure adapters and lifecycle ownership remain in
the composition root.

Add `accept_pull_request_webhook` to the Store contract. For supported PR
events, one database transaction now creates or reuses the PR session, appends
the session turn, inserts the task, inserts its outbox message, and binds the
webhook delivery to the task. A duplicate delivery returns the already-bound
task. Any exception rolls back every record. PostgreSQL additionally retains
its per-PR advisory transaction lock for turn sequence allocation.

For upgrade compatibility, a matching pre-v0.8 delivery whose `task_id` is
still null is treated as an incomplete supported intake and is completed by the
same transaction. A delivery already bound to a task remains immutable.

Malformed or unauthorized supported deliveries are validated before claiming
the delivery id, so a transient configuration correction can be retried. Events
with unsupported actions are recorded as intentionally ignored.

## Consequences

- HTTP and queue adapters keep their current `ReviewService` API.
- Use cases can be tested with fakes without starting an HTTP server or worker
  pool.
- Store adapters must satisfy atomic rollback, duplicate, payload-binding, and
  concurrent exact-once acceptance contracts.
- A committed PR task always has a committed queue intent and session turn.
- The transaction deliberately stops at the database boundary. Queue delivery
  is still asynchronous through the transactional outbox, and GitHub comments
  and repair PRs still use durable effect receipts.
- `ReviewService` still contains canary/shadow reviewer selection and runtime
  health/lifecycle wiring; these are composition concerns, not duplicated
  application decisions.
