# ADR 0028: Version and co-locate the Redis queue keyspace

- Status: accepted
- Date: 2026-08-17

## Context

The original queue used fixed Redis keys. They preserved compatibility through
v0.29, but different deployments sharing a Redis service would collide. The
fair scheduler also executes Lua across the stream, dedupe, waiting, entry, and
admission keys. Redis Cluster rejects that operation when keys map to different
slots. Merely switching the client would therefore turn publication or ACK into
a runtime `CROSSSLOT` failure.

A key migration is a delivery-protocol change. It must not silently strand
Outbox work, let an old binary consume another layout, or weaken the offline
recovery requirement that the target queue be empty.

## Decision

- An empty queue namespace retains the v1 fixed-key layout and standalone Redis
  client. This is the default and keeps existing deployments compatible.
- A validated 1-48 character `EVOAGENT_QUEUE_NAMESPACE` selects keyspace v2.
  Every queue-owned key uses the same `{review:<namespace>}` Redis hash tag,
  including the dynamic dedupe prefix and recovery marker. Independent
  environments must use independent namespaces.
- `EVOAGENT_REDIS_CLUSTER=true` requires keyspace v2 and Redis logical database
  zero. The adapter uses redis-py's topology-aware `RedisCluster`; the existing
  publish, fairness, ACK, retry, reclaim, heartbeat, and DLQ operations stay
  unchanged because all multi-key operations now target one slot.
- The first v2 process atomically installs a canonical protocol manifest.
  Consumers verify it before creating the stream group and refuse an unknown
  version or an orphaned runtime keyspace with no manifest. A recovery marker is
  the only key allowed to precede first runtime startup. Health reports cluster
  mode, namespace, and keyspace version; metrics
  expose only numeric topology state and never use namespace labels.
- Offline recovery retains the dedicated-empty-database rule for v1. For v2 it
  atomically reserves a fresh namespace and rejects any existing fixed queue
  key. Other namespaces may coexist. Dynamic dedupe keys need no separate scan:
  publication creates them atomically with a stream entry, and the stream key
  remains as the occupied-namespace guard.
- CI creates a real three-primary Redis Cluster and proves topology discovery,
  same-slot atomic delivery/dedupe, weighted fairness, live lease renewal,
  incompatible-manifest rejection, and namespace-scoped recovery.

## Consequences

EvoAgent can share a managed Redis Cluster without cross-environment key
collisions or `CROSSSLOT` failures. A single queue remains intentionally local
to one slot; cluster mode supplies topology routing and node mobility, not
tenant-by-tenant striping. PostgreSQL remains the accepted-intent source of
truth.

Namespace changes and standalone-to-cluster moves require freeze, complete
queue drain, canary startup, and end-to-end Outbox proof. No online key copy or
dual-consumption protocol is provided. A pre-v0.30 binary ignores the namespace,
so it cannot participate in a v2 rollout or rollback. Managed multi-region
routing/failover and a production-shaped cluster soak remain deployment proof,
not capabilities inferred from this adapter contract.
