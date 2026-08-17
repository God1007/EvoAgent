# ADR 0027: Add weighted tenant turns over the durable Redis stream

- Status: accepted
- Date: 2026-08-17

## Context

The database admission introduced in ADR 0026 bounds how many outstanding
reviews one tenant may own, but the single Redis Stream still exposes messages
in global arrival order. A contiguous tenant burst can therefore occupy every
next handler start before a later tenant reaches a worker. Splitting into
dynamic per-tenant streams would require replacing consumer-group ACK, pending
lease reclaim, retry, DLQ, dedupe, Outbox publication, and regional recovery at
once. A process-local scheduler would not coordinate replicas and would lose its
turn state on crash.

Fairness must also survive rolling policy changes. Raw tenant IDs must not
become metric labels or scheduler key names, and a reclaimed delivery must not
consume a second turn after its original worker already acquired one.

## Decision

- Keep the existing Redis Stream and consumer group as the durable delivery
  source. Fair scheduling is an opt-in decision immediately after
  `XREADGROUP`/`XAUTOCLAIM` and before the application handler starts.
- A bounded v1 TOML policy declares a non-sensitive stable policy ID, a default
  integer weight and at most 1000 tenant overrides, each from 1 to 100. Its
  normalized document has a canonical SHA-256. Publishers derive a SHA-256
  tenant key and attach only key, effective weight, and content digest to each
  new envelope; caller-supplied payload cannot choose those fields.
- Redis Lua atomically maintains five pieces of scheduler state: positive
  waiting counts by derived tenant key, stream-entry ownership, admitted-entry
  ownership, last granted tenant, and its current streak. When another tenant
  waits, an entry beyond its tenant's weight is appended unchanged to the stream
  tail and the old pending row is ACKed/deleted in the same script. Otherwise
  the entry moves from waiting to admitted and the handler may start.
- Successful ACK removes admitted/index state with the stream row. A transient
  failure publishes and accounts the retry before the old entry is ACKed. A
  dedicated heartbeat renews the pending idle time of every live handler, so a
  task longer than the static reclaim lease is not executed concurrently. A
  genuinely crashed process stops renewal; its reclaimed admitted entry bypasses
  a second waiting decrement. Malformed JSON releases any indexed waiting count;
  marker/index mismatch is finalized to DLQ rather than silently bypassing the
  scheduler.
- The memory backend rejects fairness configuration because it is neither
  durable nor cross-replica. All v0.29 consumers understand marked envelopes
  even when their local flag is disabled. Legacy unmarked envelopes bypass the
  new state and remain compatible.
- Fairness policy/telemetry is exposed through the optional
  `TenantFairQueuePort`; the stable `TaskQueuePort` is not expanded, so an
  existing trusted queue provider remains valid and reports neutral scheduling
  metadata until it explicitly implements the optional contract.
- Health reports configured mode, non-sensitive policy ID, and aggregate waiting
  tenants. Fixed counters record grants and tail deferrals. Tenant-authorized
  capacity inspection exposes only the caller tenant's effective weight and the
  policy ID. The content digest stays in trusted Redis evidence so public probes
  do not become a cross-tenant policy fingerprint. Prometheus never receives
  tenant or policy-map labels.

## Consequences

Tenants with queued work receive bounded weighted turns across all replicas
sharing Redis, while ACK, lease reclaim, retry, DLQ, dedupe, Outbox, and regional
recovery protocols remain intact. Weight changes are snapshotted per message, so
old and new policy generations can drain together without one replica becoming
the scheduling authority.

Fairness is by handler starts, not elapsed CPU, model tokens, GitHub latency, or
completion time. A weight-2 tenant can start two reviews per turn but a long
review can still consume disproportionate resources. Restoring turns over a
large contiguous burst requires moving skipped stream entries to the tail, so
Redis command volume can rise; durable tenant admission should remain enabled
to bound that burst and the deferral ratio is operationally visible.

The atomic decision currently assumes one logical Redis primary, optionally
backed by replicas or managed failover. Its stream and scheduler keys are not
co-located with Redis Cluster hash tags, so sharded Redis Cluster is outside the
supported topology. Cluster-aware key placement or a different coordination
design remains future work.

Deployment is two-stage: first every publisher/consumer must run v0.29 code
with marking disabled, then configuration may enable it. v0.29 disabled workers
still participate for marked messages during that second rollout. Binary
rollback below v0.29 requires disabling new marking and draining the stream,
pending entries, waiting counts, entry index, and admission index. Manually
deleting those hashes is not a supported recovery action because it can skip
turns or corrupt retry/reclaim accounting.
