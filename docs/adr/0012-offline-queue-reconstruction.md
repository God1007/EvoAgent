# ADR 0012: Offline queue reconstruction from durable task intent

- Status: Accepted
- Date: 2026-08-17

## Context

PostgreSQL is the durable task source of truth, while Redis Streams is the
delivery mechanism. After a regional loss, a restored Outbox row may already be
`published` even though the new Redis database contains no message. Starting
workers without reconciliation would strand accepted non-terminal tasks.
Blindly replaying every row against an active Redis database can instead create
duplicates and repeat external effects.

## Decision

Add a separate offline `evoagent-recover-queue` boundary.

- The application, Outbox dispatcher, and workers must be stopped.
- The operator confirms the restored PostgreSQL database name and supplies a
  unique recovery UUID.
- The target Redis logical database must be empty. Non-loopback targets require
  TLS.
- A plan covers non-terminal, non-cancel-requested tasks plus successful tasks
  with an explicitly active, incomplete delivery retry. Existing Outbox
  payloads are preserved; a missing Outbox can be reconstructed only when the
  immutable Diff still exists, while a missing successful-delivery Outbox is
  reported as unrecoverable rather than replaying the review.
- The plan contains payload hashes, counts, and a digest, never raw Diffs.
- Apply requires that exact digest, so an operator cannot accidentally approve
  one candidate set and execute another after database state changes.
- Apply atomically reserves a Redis recovery marker, then resets unpublished
  Outbox rows, creates a recovery intent beside immutable published/dead
  history, or inserts missing intent in one database transaction.
- A recovery audit record makes the same UUID idempotent only against the same
  still-reserved Redis target. Reusing it with another empty Redis is rejected.
- Terminal duplicate deliveries ACK as no-ops before release accounting,
  shadow work, or external effects.

## Consequences

A clean Redis region can be hydrated from PostgreSQL without treating Redis
backups as the sole copy of accepted intent. Existing checkpoints resume review
work and effect receipts protect completed GitHub publications.

The procedure is intentionally offline and fail-closed. It does not coordinate
traffic, provision infrastructure, or decide whether an unrecoverable task may
be abandoned. The operator must explicitly allow such tasks, and their IDs are
reported for incident handling. Cross-region automation remains a higher-level
orchestration concern.
