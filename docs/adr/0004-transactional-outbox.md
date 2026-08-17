# ADR 0004: Accept asynchronous work through a transactional outbox

- Status: Accepted
- Date: 2026-08-17

## Context

Asynchronous review creation previously inserted a task and payload, then called
the queue. A process crash or queue outage between those operations could leave
an accepted but permanently unqueued task. Retrying publication also risked
duplicate queue messages and duplicate GitHub side effects.

## Decision

EvoAgent uses a Store-backed transactional outbox:

1. task, payload, and queue intent commit atomically;
2. dispatchers claim rows with bounded ownership leases;
3. queue publication uses the task id as an idempotency key;
4. memory and Redis adapters suppress duplicate publication within the recovery
   horizon (Redis check/append/marker is one Lua operation);
5. retry exhaustion moves a row to an observable, auditable, operator-replayable
   dead state;
6. dispatcher shutdown precedes queue drain and Store/plugin shutdown;
7. external GitHub effects use stable markers/deterministic branches plus
   durable effect receipts.

Queue processing remains at-least-once. The system guarantees durable acceptance
and idempotent publication, not exactly-once computation. Domain handlers and
external effects must remain safe under retry.

## Consequences

- A committed asynchronous task always has a durable publication intent.
- Queue outages no longer have to make intake transactionally depend on Redis.
- The Store Port grows outbox/effect operations and schema version 4 adds two
  operational tables.
- Operators must monitor pending age/dead rows and own explicit replay.
- Redis dedupe keys have bounded retention; the configured retention exceeds
  the outbox retry horizon but is not an eternal exactly-once claim.
- Real PostgreSQL/Redis crash recovery still requires service-backed integration
  and chaos tests in CI.

## Alternatives rejected

- **Publish then insert task:** workers can receive a message whose task does
  not exist.
- **Insert then publish with best-effort retry in the request:** process death
  still loses the retry state.
- **Distributed transaction between SQL and Redis:** operationally heavy and
  unsupported by the chosen adapters.
- **Claim exactly-once:** Redis Streams, HTTP, and process failure do not provide
  that end-to-end semantic. Explicit at-least-once plus idempotent effects is the
  honest contract.
