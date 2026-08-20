# Operations

## Health

- `/health` reports process health and whether the queue is durable.
- `/ready` checks PostgreSQL, queue worker/heartbeat, schema version and circuit
  breakers.
- `/metrics` exposes bounded Prometheus metrics.

The in-process queue is for one-process development. Use standalone Redis
Streams when tasks must survive worker restarts.

## First checks

| Symptom | Check |
| --- | --- |
| Intake 5xx/latency | `/ready`, PostgreSQL pool, rate/admission limits, oldest outbox age |
| Stale outbox | store connectivity, dispatcher liveness, publish failures |
| Stale queue | Redis connectivity, worker and heartbeat liveness, handler duration |
| Dead letters | classify the root cause, replay one item, then a bounded batch |
| Model failures | route host/policy, circuit state, redaction/output validation counters |
| Repair/Proof failures | container availability, image, limits and bounded command output |

Every response carries `X-Request-ID`. Unexpected HTTP errors return a generic
message; correlate the identifier with structured logs. Persisted operational
failures use `operation [type=...; ref=...]` and never store exception text.

## Availability fast burn

Check `/ready`, split 5xx by release, and stop the rollout before restarting
dependencies. Preserve task IDs and traces for the incident record.

## Availability slow burn

Freeze nonessential releases, compare failure classes and tenant distribution,
and schedule remediation before the remaining error budget reaches zero.

## Intake latency

Compare intake rate with PostgreSQL pool waiters, outbox age and CPU. Shed burst
traffic through configured admission limits before increasing concurrency.

## Queue or outbox stale

PostgreSQL retains committed intent. Restore the failing dispatcher/Redis path;
do not manually alter rows or stream entries.

## Dead letters

Fix the root cause, replay one item, verify its terminal state and external
effects, then replay a bounded batch.

## Review failures

Separate policy rejection, reviewer failure, model transport and Skill failure.
Do not weaken evidence gates to restore throughput.

## Repair outcomes

Inspect the container image, command, timeout and resource limits. A verifier
abstention is safer than publishing an unproven edit.

## Quality feedback

Sample false positives, missed issues and bad fixes by rule. Change a rule only
with a reproducible evaluation case.

## History retention

Keep retention disabled until policy approval. If enabled maintenance stalls,
check PostgreSQL locks and batch limits; never broaden deletion predicates.

## Tenant review capacity

Sustained rejection means a tenant owns too many non-terminal reviews. Resolve
or finish existing tasks before raising the bound.

## Queue recovery

A live Redis handler heartbeats its pending entry. Do not increase the reclaim
threshold to hide heartbeat failures. Restore Redis connectivity; only a dead
handler should become reclaimable. Never edit stream/outbox records manually.
Use the audited DLQ replay or offline reconstruction command.

## Deployments

1. back up PostgreSQL;
2. run migrations against a disposable copy;
3. deploy one instance and verify `/ready` plus one review;
4. deploy remaining instances;
5. watch availability, review success, outbox age, queue/DLQ depth and tenant
   admission rejections.

Roll back application code only if it understands the active forward-only
schema. Redis carries no irreplaceable state.

## Security defaults

- Require JWT authentication before binding beyond loopback.
- Trust forwarded addresses only from explicit ingress CIDRs.
- Keep GitHub, JWT and model secrets in a secret manager.
- Require a container for untrusted repair verification.
- Keep model and GitHub egress on exact host allowlists.
- Leave history retention disabled until backup and legal policies approve it.

For restore procedures see [disaster recovery](disaster-recovery.md).
