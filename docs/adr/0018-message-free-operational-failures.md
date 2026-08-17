# ADR 0018: Make operational failure records message-free

- Status: Accepted
- Date: 2026-08-17

## Context

ADR 0017 removed raw exception text from the public HTTP boundary, but the same
text could still cross less visible boundaries: task error and Trace rows,
graph Checkpoints, evaluation failure cases, Agent messages, Queue/DLQ entries,
Outbox/effect receipts, readiness responses, shadow-review audit details,
proof/verifier results, plugin lifecycle failures, and OpenTelemetry's automatic
exception event. Exception messages can contain credentials, connection strings,
provider response bodies, file paths, source fragments, or attacker-controlled
input. Hiding them at HTTP while retaining them throughout the system is not a
complete security boundary.

Operators still need a bounded signal that groups failures by class and code
site without retaining the message or stack values.

## Decision

- Operational failures have one allowlisted textual grammar:
  `operation [type=qualified.Exception; ref=16-hex]`. Arbitrary operation labels
  and preformatted strings are rejected rather than trusted.
- `type` is a sanitized, length-bounded qualified exception class.
- `ref` is the first 16 hexadecimal characters of SHA-256 over the exception
  type and up to 12 traceback module/function/line locations. The input never
  includes the exception message, arguments, locals, request values, or source
  content. Identical stacks and types therefore correlate even when their
  messages contain different values.
- Review Harness, Agent coordinator, Queue/DLQ, Outbox/effect, readiness,
  shadow/evaluation, Proof/Verifier, plugin listener/lifecycle, and HTTP error
  logging construct these summaries at their component boundary.
- SQLite and PostgreSQL validate failure-bearing task, Trace, Checkpoint,
  execution-case, Agent, Outbox, and effect writes again. An untrusted string
  reaching a Store Port becomes `type=unknown` with a fixed unclassified
  reference. This is defense in depth for future call paths.
- OpenTelemetry SDK exception capture and automatic status-on-exception are
  disabled for application spans. EvoAgent emits only `error.type`, `error.ref`,
  and a message-free failure event before re-raising.
- Forward migration 10 replaces known legacy operational exception fields with
  `type=legacy` summaries. Redis DLQ values cannot be migrated transactionally,
  so they are normalized when read, replayed, or delivered to callbacks.
- Explicit reviewed client/configuration errors remain human-readable. The
  credential-redacted model usage ledger and deliberately captured sandbox
  command output remain separate, access-controlled data-retention surfaces.

## Consequences

Routine operational records, health APIs, plugin callbacks, and spans no longer
become secondary stores for secret-bearing exception strings. Security tests
inject distinct credentials across every persistence and telemetry path and
require them to be absent. SQLite/PostgreSQL migration parity is kept in one
checksummed logical migration.

The contract trades raw convenience for safer correlation. Operators must use
`type + ref`, the application image/version, request/task IDs, dependency
metrics, and source ownership to diagnose a site. A reference can change when
line numbers or call stacks change, can collide, and is not an incident ID,
authorization token, tenancy key, or idempotency key. Historical raw messages
in the migrated fields are intentionally discarded rather than archived.
