# Production operations and SLO runbook

This runbook turns EvoAgent's telemetry into operator actions. The source of
truth for objectives is [`ops/slo.toml`](../ops/slo.toml); Prometheus recording
and alert rules live in
[`ops/prometheus/evoagent.rules.yml`](../ops/prometheus/evoagent.rules.yml).

## SLO contract

| Objective | 30-day target | Indicator |
| --- | ---: | --- |
| API availability | 99.9% | Non-probe requests whose response is not 5xx |
| Async intake latency | 99% within 500 ms | Webhook and async-review intake histogram |
| Review success | 99% | Successful / all terminal review executions |

`429` is deliberate client-level throttling and does not consume the 5xx
availability budget. A heavy-gate `503` does consume it: admitted capacity is
part of the service contract. Authentication/validation 4xx responses do not
consume availability but must be monitored for client regressions separately.

Evaluate the current Prometheus window in CI, a release gate, or an incident:

```bash
evoagent-slo --catalog ops/slo.toml \
  --prometheus-url https://prometheus.internal.example \
  --allowed-host prometheus.internal.example
```

The command exits `0` when all objectives pass, `1` on an SLO breach, and `2`
when an objective has insufficient samples. `--allow-no-data` is appropriate
only for a new, intentionally idle environment. Credentials come from
`EVOAGENT_PROMETHEUS_BEARER_TOKEN`; redirects and environment HTTP proxies are
disabled and non-loopback endpoints require HTTPS.

Run one `EVOAGENT_WEB_WORKERS=1` process per production pod and scale pods
horizontally. The built-in metric registry is process-local, so scraping one
shared `SO_REUSEPORT` socket cannot aggregate several local worker processes.
Prometheus should attach deployment/cluster/region labels at scrape time and
aggregate across pods in queries.

## Trusted proxy client identity

`X-Forwarded-For` is ignored by default. Set `EVOAGENT_TRUSTED_PROXY_CIDRS` only
to the canonical CIDRs of proxies that can connect directly to EvoAgent. The
resolver starts at the kernel-supplied socket peer, consumes the chain from
right to left while each current hop is trusted, and stops at the first
untrusted address. An attacker-controlled prefix farther left is never used for
rate limiting or access logs. Empty hops, hostnames, addresses with ports, more
than 32 hops, or more than 4096 header bytes fail closed to the socket peer.

During ingress rollout, send two test requests with different client addresses
through the real proxy and confirm `client_source=forwarded`, the expected
`client`/`peer`, and independent rate-limit buckets. Direct requests with the
same headers must produce `client_source=ignored`. Alert or investigate growth
in `http_forwarded_invalid_total`; compare the proxy's append/overwrite policy
without logging the raw header. Never configure `0.0.0.0/0` or `::/0`—startup
rejects both—and do not copy an entire cloud public range when only private
ingress addresses can reach the service.

## Availability fast burn

This alert means the 99.9% monthly error budget is being consumed at more than
14.4× over both 5-minute and 1-hour windows.

1. Check `/ready` and split 5xx rate by pod, deployment version, and region.
2. Stop rollout or route traffic back to the last healthy version if failures
   correlate with a release.
3. Check Store/Redis readiness, circuit-breaker state, pool availability,
   Outbox age, and worker liveness before restarting anything.
4. Preserve failed task IDs and traces; do not replay DLQ/Outbox items until the
   dependency or code fault is fixed.
5. Record incident start/end, affected requests, budget consumed, and rollback
   decision.

## Availability slow burn

This is a sustained 6× budget burn over 30-minute and 6-hour windows. Open an
incident ticket, freeze nonessential releases, compare error types and tenant
distribution, and schedule remediation before the remaining budget reaches
zero. Page immediately if the fast-burn alert also fires.

## Correlated HTTP failures

Every response carries `X-Request-ID`. For an unexpected failure, the JSON body
contains only `internal server error` plus that identifier. Search structured
container logs for the same `request_id` and `event=http_internal_error`, then
use the bounded `error_type`, deployment metadata, component telemetry, and task
trace to locate the failing dependency. The public edge intentionally records no
exception message, traceback, or query string.

Do not temporarily enable raw client errors during an incident. If additional
detail is required, add it to access-controlled component tracing with explicit
redaction. Treat caller-provided request IDs as untrusted labels; never use them
to authorize a replay, reconciliation, or tenant operation.

## Operational failure references

Asynchronous and component failures use this message-free shape:

```text
task delivery failed [type=builtins.RuntimeError; ref=4d8f6a19c3e57021]
```

`type` identifies the bounded exception class. `ref` hashes that class and the
traceback's module/function/line locations; it never hashes the exception
message, arguments, locals, request values, or source content. Group the same
`type + ref` within one image/version to find a common failure site, then use
deployment metadata, request/task IDs, dependency metrics, and code ownership
to investigate. A reference is neither globally unique nor an authorization or
idempotency key, and source-line changes can change it between releases.

Task/Trace/Checkpoint, Agent failure, Queue/DLQ, Outbox/effect, readiness,
shadow/evaluation failure, Proof/Verifier, plugin lifecycle, and OpenTelemetry
paths must not be temporarily switched back to raw exception messages. Schema
version 10 replaces legacy values in these persisted operational fields. The
model usage ledger's credential-redacted provider diagnostics and authorized
sandbox command output are separate, access-controlled retention surfaces; do
not copy them into general task or readiness records.

## Intake latency

1. Compare intake RPS, p99, `outbox_oldest_age_seconds`, Postgres pool available,
   and CPU saturation.
2. If DB pool waiters rise, confirm total pool demand across pods is below the
   Postgres connection budget before increasing a pool.
3. If only SQLite is configured, migrate production traffic to PostgreSQL;
   SQLite intake is a development topology.
4. Shed burst traffic with the existing token bucket instead of increasing
   unbounded HTTP concurrency.

## Queue or Outbox stale

- **Outbox stale, queue healthy:** inspect dispatcher error, Store connectivity,
  and `outbox_publish_failures_total`. A committed task is safe in the Store;
  restart the dispatcher only after confirming another owner is not publishing.
- **Queue stale:** inspect Redis health, worker count, task duration, and lease
  reclaim. Scale workers only if downstream GitHub/model/DB capacity can absorb
  them.
- **Lease heartbeat failure:** every live Redis handler periodically resets its
  pending-entry idle time. Check readiness `lease_heartbeat_running`, Redis
  latency/timeouts, and `queue_lease_heartbeat_failures_total`. Do not increase
  the reclaim threshold to hide a broken heartbeat; restore Redis connectivity
  so only a truly lost process becomes eligible for `XAUTOCLAIM`.
- **Regional reconstruction:** a `FAILED` async task with an active durable
  admission is still retry-managed and is included in an offline recovery plan;
  a synchronous failed task whose slot was released remains terminal.
- Never edit queue/Outbox rows manually. Use `/v1/outbox/replay` or
  `/v1/queue/dead-letters/replay`, which audit the operation and preserve
  idempotency keys.

## Dead letters

Classify the error as permanent input/policy failure or transient infrastructure
failure. Fix the root cause, replay one item, verify its terminal state and
external effects, then replay the bounded batch. A growing DLQ is not resolved
by deleting entries; export task IDs and retain the audit trail.

## Review failures

Split failures by reviewer, model route, repository, and error type using task
traces and the metadata-only model ledger. Check policy rejections separately
from execution failures. Roll back a candidate Prompt/Skill if the failure spike
is lane-correlated; do not weaken evidence or repair gates to restore throughput.

## Model route capacity

Query `/api/model-routes/capacity` in the affected tenant/repository scope. A
high concurrency rejection count with long-lived leases indicates saturated or
stuck provider calls; a rate rejection means the declared fixed-minute ceiling
was reached. Exact counters are tenant-visible only for a route bound solely to
that tenant; investigate a `shared-redacted` pool through restricted platform
storage/telemetry. Confirm the provider contract before changing either limit.
Do not delete leases manually: a crashed owner's lease expires after
`EVOAGENT_LLM_CAPACITY_LEASE_SECONDS`, while a live call must retain its slot.

If fallback is also saturated, reduce intake or restore provider capacity before
raising worker count. Weight recommendations are declared-capacity ratios, not
traffic forecasts. Apply them only through reviewed topology and redeployment;
never treat the report as authorization for a live database edit. Preserve
stable route IDs across rolling releases so old and new pods share one pool.

## Model economics

`model_input_tokens_total`, `model_output_tokens_total`, and
`model_cost_micros_total` include successful calls and failed output-contract
checks when provider usage is already known. Transport failures without usage do
not invent cost. Active and shadow lane counters are separate fixed metric names,
and request latency is split into active/shadow histograms without route,
repository, or tenant labels.

Use `evoagent:cost:model_micros_per_terminal_review_30m` as an operational trend,
not an invoice: pricing is configured micro-units and providers may reconcile
usage later. A budget-rejection alert means at least one repository reached its
explicit ceiling. Identify it through the tenant-authorized model-usage ledger;
do not add tenant/repository labels to Prometheus or raise the limit before
confirming ownership and expected workload.

## Repair outcomes

Repair attempts terminate as published, safely abstained, verification-blocked,
or failed. Abstention means no deterministic allowlisted transformation applied;
it is not an infrastructure failure. A verification block means a proposed
change failed compilation/tests and was correctly withheld. Investigate the
configured verifier/container and recent rule changes before changing a gate.
Never lower verification requirements to improve the publication ratio.

## Quality feedback

Feedback counters use four fixed names: accepted, false positive, missed issue,
and bad fix. The 24-hour negative ratio combines the latter three only after a
minimum sample threshold in the alert. It is a triage signal, not a statistically
unbiased accuracy estimate: operator reporting is selective. Sample affected
cases from tenant-authorized failure records, classify rule/model/language
slices offline, and add independently adjudicated cases to the evaluation
pipeline before changing prompts or thresholds.

## Plugin runtime

`plugin_runtime_ready=0` means the capability graph is stopping or failed.
Inspect startup dependency/cycle errors, the active Profile, and the trusted
plugin allowlist. Roll back the plugin package or Profile as one deployment;
live global hot-swap is intentionally unsupported.

## Tenant review capacity

`EVOAGENT_TENANT_MAX_ACTIVE_REVIEWS` is a uniform hard limit on outstanding
review admissions for each tenant across every replica sharing the database.
The default `0` does not reject work, but v0.28 still records durable admission
slots so operators can observe actual occupancy before enabling a limit. A
capacity rejection returns `429` with `Retry-After` set from
`EVOAGENT_TENANT_CAPACITY_RETRY_SECONDS`, records a tenant-scoped
`review.capacity-rejected` audit event, and creates no task, session turn, or
Outbox intent. A rejected webhook delivery claim remains safely retryable.

Schema migration 14 is additive and backfills non-terminal tasks. Apply it and
complete an all-pod v0.28-or-newer rollout before setting a non-zero limit:
older writers neither take the tenant advisory lock nor emit admission
generations. Also drain or explicitly resolve any pre-v0.28 Redis retry backlog;
a historical task already marked `FAILED` cannot be distinguished from a
terminal synchronous failure during migration. Enable the limit only after the
authorized `/api/tenant-review-capacity` view and
`review_admission_slots_active` agree with the expected backlog.

The Store transaction owns these lifecycle rules:

- a new REST or webhook review reserves a slot before atomically committing its
  task, Diff/session state, and Outbox message;
- synchronous failure releases immediately, while asynchronous failure keeps
  the slot through queue retries and offline queue reconstruction;
- success, cancellation, or final dead-letter disposition releases the slot;
- manual resume atomically reacquires capacity and creates a unique Outbox
  intent; a monotonically increasing admission generation prevents a late
  callback from an older delivery from releasing the resumed slot.

Investigate `EvoAgentTenantReviewCapacitySaturated` with the fixed-cardinality
rejection ratio, global active-slot gauge, authorized tenant capacity view,
Outbox age, queue age, and DLQ depth. First decide whether occupancy is valid
load or stuck delivery. Repair the dependency and use the audited Outbox/DLQ
replay APIs. For abandoned queued work, request cancellation through the
application path and, if delivery is no longer live, replay it so a worker can
commit the final `CANCELLED` state and release the slot. Do not edit
`task_admissions` or task state with SQL. Increase
the limit only through reviewed deployment configuration after checking worker,
database, model, and GitHub capacity.

This control alone bounds durable occupancy; it does not order workers. Unless
the tenant-fair scheduler below is enabled, Redis workers still dequeue admitted
work in stream order. Per-tenant details stay behind tenant authorization rather
than becoming Prometheus labels.

## Redis cluster queue

The default empty `EVOAGENT_QUEUE_NAMESPACE` preserves the v1 single-node keys
used through v0.29. Set a stable, non-sensitive namespace such as `prod-eu1` to
select the v2 layout. Every stream, DLQ, dedupe, fairness, protocol, and recovery
key then contains the same `{review:<namespace>}` hash tag. This both isolates
environments and lets every Lua/transaction boundary execute in one Redis
Cluster slot. Set `EVOAGENT_REDIS_CLUSTER=true` only with a v2 namespace and a
Redis URL using logical database 0. All advertised cluster node addresses must
be reachable from every application pod; use `rediss://` and the managed
service's authenticated CA configuration outside a trusted private network.

The first v2 process atomically creates a canonical protocol manifest inside the
namespace. Later processes compare it before creating the consumer group and
fail startup on an unknown protocol/keyspace version. A Stream/DLQ/fairness key
without a manifest is treated as an incompatible orphan rather than adopted;
only a recovery Epoch may legitimately precede first startup. `/ready` exposes
`redis_cluster`, `queue_namespace`, and `keyspace_version`; Prometheus exports
fixed gauges without turning the namespace into a label.
`EvoAgentQueueKeyspaceVersionMixed` means replicas behind one scrape scope do
not agree: stop the rollout, determine which deployment still serves v1, and do
not move or delete Redis keys manually.

Changing the namespace is a queue cutover, not an in-place rename:

1. deploy v0.30 to every publisher/consumer while the namespace remains empty;
2. stop intake and Outbox publication, then drain stream, pending entries,
   retries, DLQ decisions, and fairness indexes in the old keyspace;
3. configure one reviewed namespace on every replica and start a canary against
   standalone Redis first; verify protocol version 1, keyspace version 2, and an
   end-to-end Outbox delivery;
4. to move from standalone Redis to Redis Cluster, repeat the freeze/drain,
   switch the URL and cluster flag together, and verify all cluster slots are
   covered before reopening traffic;
5. roll back only after draining the currently active namespace. A pre-v0.30
   binary ignores the namespace and must never run concurrently with v2.

Use a different namespace for each environment. Sharing a namespace shares the
stream, consumer group, dedupe decisions, fairness state, and recovery epoch.
Redis Cluster improves sharding and managed availability but does not by itself
prove regional failover: run the queue reconstruction procedure against a fresh
namespace after restoring PostgreSQL. The v2 recovery reservation checks only
the target namespace, so unrelated cluster workloads do not make the target
appear non-empty; an existing protocol/stream/DLQ/fairness key still fails
closed. Dedupe entries are created atomically with the stream, so an occupied
stream is the recovery guard for their dynamic key family.

## Tenant fair scheduling

`EVOAGENT_QUEUE_FAIR_SCHEDULING=true` enables weighted round-robin dispatch on
the production Redis Streams backend. It does not change Outbox publication,
consumer-group ACK, retry/DLQ, or lease-reclaim durability. Every new envelope
receives a SHA-256 tenant key, an integer weight from 1 to 100, and the
content-addressed policy digest. Redis Lua atomically grants up to that tenant's
weight in one turn or moves the entry to the stream tail while another tenant is
waiting. Raw tenant identifiers never enter scheduler keys or Prometheus labels.

On the legacy v1 layout the scheduler requires a single logical Redis primary.
The v2 namespace places its stream and scheduler indexes in one cluster slot, so
the same Lua decision is supported through the Redis Cluster client. This is
intentional per-queue slot locality: a namespace can move between cluster nodes,
but one review queue is not striped across all masters. Use separate namespaces
only for truly independent environments, not to shard tenants and bypass global
fairness.

The optional `EVOAGENT_QUEUE_TENANT_WEIGHTS_FILE` is bounded v1 TOML:

```toml
version = 1
id = "service-tiers-v1"
default_weight = 1

[tenants]
"standard-tenant" = 1
"priority-tenant" = 2
```

Use a non-sensitive stable `id` for rollout comparison and the smallest weights
that express the service tier. A weight controls
dispatch starts, not CPU seconds or model cost: a tenant whose reviews run much
longer can still consume more worker time. The tenant-authorized
`/api/tenant-review-capacity` response exposes only that tenant's effective
weight and the non-sensitive policy ID. Health exposes the ID, configured mode,
and aggregate waiting-tenant count. The content SHA-256 remains inside the
trusted Redis envelope as execution evidence rather than becoming a public
cross-tenant configuration fingerprint.

Roll out in two stages. First deploy v0.29 or newer to every queue publisher and
consumer with fairness disabled. Only after all old workers are gone may the
flag be enabled through reviewed configuration; v0.29 workers that are not yet
enabled still recognize and coordinate marked envelopes during the configuration
rollout. Legacy unmarked backlog remains compatible and drains in original
stream order. Before rolling back to a pre-v0.29 binary, disable new marking and
wait until the Redis stream, pending set, `fair_waiting_tenants`, and fairness
entry/admission hashes are empty. Do not delete scheduler hashes to force a
rollback: that can bypass turns or strand accounting.

An admitted entry moves from the waiting hash to an admission hash before its
handler starts. A dedicated live-delivery heartbeat resets its Redis pending
idle time, preventing a legitimate task longer than the static lease from being
claimed concurrently. If the process actually crashes, the heartbeat stops and
`XAUTOCLAIM` recognizes the retained admission without decrementing the tenant
twice. A transient failure publishes a newly tracked retry before ACKing the old
entry; final DLQ uses the existing task admission generation rules. Invalid
marker/index correspondence fails closed to DLQ instead of bypassing fairness.

Investigate `EvoAgentTenantFairnessChurnHigh` with
`evoagent:ratio:queue_fair_deferrals_15m`, queue age/depth, waiting tenants,
worker concurrency, admission saturation, and the current policy ID. A high
ratio normally means one tenant placed a long contiguous burst ahead of other
tenants, requiring bounded tail moves to restore turns. Keep the per-tenant
durable admission limit enabled to bound that work. If queue age also breaches,
reduce intake or add proven downstream capacity; raising weights merely moves
latency between tenants. Never add tenant IDs as metric labels.

## History retention

Operational-history deletion is disabled by default. Set
`EVOAGENT_HISTORY_RETENTION_DAYS` above zero only after the retention period,
backup/restore evidence, legal holds, and incident-forensics requirements have
been approved. Schema migration 13 is additive and deletes nothing. During a
rolling PostgreSQL deployment, apply the migration first, complete the rollout
so every writer is v0.27 or newer, and only then enable retention: all writers
must participate in the new per-session coordination lock before pruning can be
considered safe.

Each maintenance run is bounded by
`EVOAGENT_HISTORY_PRUNE_BATCH_SIZE` and ten batches. The batch size limits Trace
rows and session turns per transaction; one session turn can contain multiple
findings, so choose a conservative value from measured production snapshots.
The Store transaction enforces these invariants:

- active-task Trace is never removed, and a terminal task always retains its
  latest event;
- the latest completed turn in a PR session remains a future-turn anchor;
- a completed snapshot remains while an out-of-order pending turn could still
  require it as the immediately preceding completed state;
- task `trace_pruned_at` and timeline `findings_retained=false` markers make
  deliberate pruning distinguishable from an originally empty result.

Check `/health.retention`,
`retention_trace_events_pruned_total`,
`retention_session_findings_pruned_total`, and `retention_failures_total` after
enablement. `EvoAgentRetentionMaintenanceStalled` fires when no successful run
has completed within twice the configured interval (with a ten-minute floor).
Compare `last_error_type` with Store health and migration compatibility; it is
intentionally message-free. If the alert persists, disable the setting through
the reviewed deployment configuration, preserve database/metric evidence, and
fix the adapter or capacity problem before re-enabling it. Do not delete rows or
edit prune markers with ad-hoc SQL. Retention is not a backup, VACUUM strategy,
native PostgreSQL partition policy, or legal-hold system.

## Proof Runner

Use `proof_inconclusive_total / proof_runs_total` and runner capacity logs to
distinguish bad reproductions from infrastructure uncertainty. Runner errors
must never be reclassified as failed reproductions. Follow
[`remote-proof-runner.md`](remote-proof-runner.md) for isolation, key rotation,
artifact retention, and replica limits.

For more than one Runner replica, require shared replay and alert when
`/readyz` is not ready. A replay Redis outage is not a reason to bypass nonce
claims or switch to memory; stop proof traffic and retain L1 uncertainty. During
HMAC rotation, verify all Runners accept current+previous IDs before switching
clients, wait for the request/replay drain, then remove the previous key. Never
reuse a retired key ID for different key material.

For regulated proof retention, use a dedicated versioned S3 bucket with Object
Lock, set `REQUIRE_ARTIFACTS=true`, and monitor both `artifact_ready` and the
Runner `/readyz` result. Prefer `COMPLIANCE`; governance mode is an operational
test mode because principals with bypass permission can remove protection. The
Runner identity should not have `DeleteObject` or `BypassGovernanceRetention`.
Exercise a real conditional write, checksum/version/retention inspection, and
restore in each target account/region before treating configuration health as
compliance evidence. Do not shorten `RETENTION_DAYS` during a rollout; existing
objects are extended when necessary and are never shortened by EvoAgent.

## Release checklist

1. Ruff, mypy, unit/contract tests, dependency audit, wheel build pass.
2. PostgreSQL/Redis/container boundary CI passes without skipped external tests.
3. Performance regression gates and SLO catalog validation pass.
4. Schema migration job completes before new application pods become ready.
5. Canary health and error budget are observed before widening traffic.
6. Rollback artifact, previous image digest, and migration compatibility are
   recorded.
7. `evoagent-dr` restored into a disposable database, content fingerprints
   matched, cleanup succeeded, and the report is within declared RPO/RTO.

The complete procedure and evidence contract are in the
[disaster-recovery runbook](disaster-recovery.md). Database recoverability is
automated together with offline Redis task reconstruction; regional
infrastructure/routing failover and a production-shaped soak remain required
before claiming full service disaster-recovery readiness.
