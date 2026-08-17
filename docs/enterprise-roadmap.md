# EvoAgent Enterprise Engineering Roadmap

This roadmap separates capabilities already proven in the repository from work
that is still required before claiming production-grade enterprise readiness.
It is an execution plan, not a marketing checklist.

## Current maturity (v0.29.0)

| Area | Status | Evidence / boundary |
| --- | --- | --- |
| Reproducible engineering | Implemented | Hash locks, Ruff, mypy, tests, coverage gate, package build, dependency audit |
| Application composition | Implemented | Trusted plugin graph, typed service-definition/provider/consumer seams, bounded content-addressed Profile layers, Scope, rollback, and reverse shutdown |
| Application boundaries | Implemented baseline | Focused Review/Webhook/Session/Repair/Policy/Model Usage use cases behind a compatible facade; evolution is an independent capability |
| Review extensibility | Implemented advanced baseline | Multi-provider `review.reviewer` contributions feed the unchanged Engine; Dynamic Skill reload is transactional and content-addressed, with optional mandatory container execution |
| Repair extensibility | Implemented | Independent `fix.rule` providers; verifier and publication gates remain centralized |
| Local durability | Development only | SQLite + memory queue is explicitly non-durable |
| Production persistence | Implemented advanced baseline | PostgreSQL + Redis Streams, migration CLI, Outbox, ACK/lease/DLQ, isolated backup/restore, queue reconstruction, and mandatory real-service CI; managed PITR remains pending |
| Graceful lifecycle | Implemented | Readiness drain plus bounded queue drain before Store/plugin shutdown |
| Multi-tenancy/governance | Implemented advanced baseline | JWT, RBAC, tenant/repository authorization, audit, canary/shadow/rollback, database-atomic cross-replica tenant review admission, and opt-in Redis weighted tenant turns with content-addressed policy plus retry/reclaim accounting; runtime-cost-aware shares remain pending |
| Model governance | Implemented advanced baseline | Replaceable gateway, scoped policy routing/residency, deterministic weighted active routes, bounded fallback, isolated candidate shadows, GitOps promotion gates with offline evidence, shared hard capacity leases/rate windows, read-only capacity weight recommendations, per-route breakers, redaction, egress/output limits, total/shadow atomic budgets, metadata-only ledgers, and conservative crash reconciliation; automatic capacity inference remains pending |
| Strong untrusted execution | Implemented advanced baseline | Replaceable remote Proof Runner, mutually authenticated evidence manifests, container-only jobs, cross-replica Redis nonce claims, dual-key rotation, and pluggable local/S3 Object Lock artifacts; a microVM executor and provider-backed compliance drill remain pending |
| Service-level operations | Implemented advanced baseline | Fixed-cardinality availability/latency/success SLIs plus model economics, route/tenant-review capacity saturation, fair-scheduler churn, repair outcomes, and feedback trends; versioned 30-day SLO catalog, multi-window burn/quality alerts, queue/Outbox age, DLQ depth, dashboard, runbooks, and hardened Prometheus evaluator |
| HTTP edge security | Implemented advanced baseline | Validated/generated request correlation, explicit client-safe 4xx types, bounded list reads, generic 5xx envelopes, query-free structured access logs, consistent hardening headers, no interpreter-version disclosure, and spoof-resistant trusted-proxy client identity for admission/logging |
| Operational failure security | Implemented baseline | Allowlisted message-free failure summaries, stable code-location references, persistence-adapter enforcement, legacy-data migration, and exception-message-free OpenTelemetry/plugin/proof paths |
| Quality evidence | Governance baseline implemented | Reproducible synthetic regression plus blind dual-annotation/adjudication compiler, rights/content/split/evidence audit, per-language/CWE/rule slices, and confidence calibration; production gate remains blocked until a real approved corpus is supplied |
| HA/DR operational proof | Implemented recovery baseline | SQLite/PostgreSQL isolated restore validates schema/content/application/RPO/RTO; an offline audited epoch reconstructs incomplete PostgreSQL/Outbox intent only into empty Redis and is proven against real CI services; managed PITR and regional routing exercise remain pending |
| Operational data lifecycle | Implemented baseline | Opt-in bounded retention removes eligible terminal Trace history and superseded session snapshots while preserving recovery/continuity anchors and durable deletion markers; native partitioning, managed lifecycle, and long-soak evidence remain pending |

## Phase 2 — Ports, persistence, and integration proof

### 2.1 Explicit domain ports — baseline implemented

The first implementation replaces the Store/Queue/CodeHost `Any` and concrete
adapter boundaries with focused Protocols:

- `TaskExecutionStore`, `GovernanceStore`, `SessionStore`, `EvaluationStore`;
- `TaskQueuePort`, `CodeHostPort` (the `ModelGatewayPort` remains in phase 4);
- contract tests that every SQLite/PostgreSQL and Memory/Redis implementation must pass.

Acceptance:

- application and domain modules depend on ports, not concrete adapters;
- mypy checks both production adapter implementations against the same contracts;
- a fake adapter can run service use-case tests without monkey-patching internals.

Evidence: `evoagent/ports.py` is consumed by the harness, reviewer graph,
authentication, evolution, rollout, alerting, fixer, service capability graph,
and store factory. `tests/test_adapter_contracts.py` runs the same behavior
contract against SQLite/PostgreSQL and Memory/Redis (production backends are
enabled by explicit test URLs). This extraction also found and fixed missing
PostgreSQL shadow-observation/automatic-promotion parity.

### 2.2 Versioned database migrations — implemented with isolated restore proof

Replace constructor-time ad-hoc schema mutation with an explicit migration
history and startup compatibility check.

Acceptance:

- forward migration from the last supported release is tested;
- startup refuses a newer unsupported schema instead of guessing;
- migration rollback/restore procedure is documented;
- PostgreSQL backup and restore is exercised in CI or a scheduled environment.

Implemented evidence: one immutable SQLite/PostgreSQL catalog records version,
name, checksum, and timestamp; SQLite and PostgreSQL acquire migration locks;
startup rejects newer, discontinuous, or modified histories; an unversioned
legacy database is adopted without row loss; an injected DDL failure proves
transaction rollback. `evoagent-migrate` supports a separate deployment job and
`/ready` exposes the schema version. The mandatory PostgreSQL integration job
now performs the isolated backup/restore proof described in Phase 5; managed
PITR remains a deployment-provider responsibility.

### 2.3 Transactional task/outbox semantics — implemented baseline

Close the failure window between writing a task record and publishing a queue
message. Introduce an outbox with idempotent dispatcher and consumer keys.

Acceptance:

- process death after task commit but before queue publish cannot orphan work;
- duplicate delivery cannot publish duplicate comments or fix PRs;
- chaos tests kill workers at each persistence/ACK boundary and prove recovery.

Implemented evidence: task + Diff + outbox commit atomically; a leased dispatcher
publishes by stable task-id key; Memory and Redis adapters deduplicate publication
(Redis uses atomic Lua); expired publisher leases are reclaimable; attempt
exhaustion becomes observable/replayable `dead`; readiness and metrics expose
the path. Effect receipts, comment markers, and deterministic repair branches
make GitHub publication retry-safe. In-process fault injection covers transaction
rollback and the publish-before-mark crash. The mandatory adapter CI matrix now
adds real PostgreSQL/Redis and a consumer-process death before ACK.

### 2.4 Mandatory adapter integration matrix — implemented baseline

Add CI jobs for PostgreSQL, Redis Streams, GitHub HTTP fixtures, and containerized
Verifier execution. Remove those modules from coverage exclusions only after the
boundary suites exist.

Acceptance:

- Redis lease reclaim and DLQ replay are tested across process restart;
- PostgreSQL pool exhaustion and reconnect behavior are tested;
- wheel-installed resources, health/readiness, and shutdown drain run in the
  built container image.

Implemented evidence: `.github/workflows/ci.yml` provisions PostgreSQL 16 and
Redis 7 for every pull request; runs the shared adapter suite plus pool
exhaustion/replacement, Redis reconnect, cross-process reclaim, dedupe, and DLQ
tests plus weighted tenant turns and live-delivery lease renewal; drives the
GitHub client over a real local HTTP fixture; executes the
Verifier against Docker; smoke-tests the installed wheel's web/Skill resources;
and runs an async review through the built image, Postgres outbox, Redis Stream,
and worker before a bounded SIGTERM shutdown. PostgreSQL remains excluded from
the single-process unit coverage denominator until its entire broad Store
surface (not only critical contracts) has boundary coverage; the external job
is nevertheless a required pass/fail merge gate.

## Phase 3 — Application decomposition and policy scopes — implemented baseline

`ReviewService` remains the public API-compatible composition facade, while
business orchestration is split into independently testable application
objects:

```text
ReviewApplication
  ├── ReviewUseCases
  ├── WebhookUseCases
  ├── RepairUseCases
  ├── SessionUseCases
  ├── PolicyUseCases
  └── EvolutionEngine (independent capability)
```

Implemented repository policy dimensions:

- allowed reviewers and FixRules;
- allowed LLM providers and models;
- repository-specific Diff size;
- enable/disable, auto-fix, and comment-publication decisions.

The policy is versioned and audited. Accepted tasks retain the exact policy
snapshot used for admission, while current disable/comment restrictions remain
emergency kill switches. Cost/token quotas, data residency, redaction, and
tenant-specific provider credentials intentionally remain in Phase 4 because a
model gateway is required to enforce them rather than merely store them.

GitHub PR intake was also made one Store unit of work: delivery binding, session
turn, task, and outbox commit atomically. Duplicate and concurrent deliveries
resolve to one task; fault injection proves that an exception leaves no partial
delivery, session, task, or queue intent.

Acceptance:

- focused application Ports and immutable options pass mypy;
- direct use-case tests run without HTTP or plugin startup;
- SQLite/PostgreSQL share duplicate, concurrency, and rollback contracts;
- repository policy changes are audited, versioned, and tenant scoped;
- public service/API behavior remains backward compatible.

Evidence: `evoagent/application/`, the focused Protocols in
`evoagent/ports.py`, `tests/test_application_use_cases.py`, the shared adapter
contracts, and
[`ADR 0006`](adr/0006-use-case-boundaries-and-atomic-webhook-intake.md).

## Phase 4 — Model gateway and execution isolation

### Model gateway — governed multi-route baseline implemented

- provider routing, fallback, retry budget, and per-model circuit breakers;
- request/token/cost accounting by tenant and repository;
- structured-output validation, redaction, data-retention policy, and egress allowlist;
- offline/shadow evaluation before route changes.

Implemented evidence: production reviewers receive `ModelGatewayPort` rather
than endpoint credentials; task context is propagated into parallel specialists;
schema version 6 records metadata-only usage; estimated input plus maximum output
is atomically reserved per tenant/repository/UTC day; concurrent overspend is
covered by the shared SQLite/PostgreSQL contract; success reconciles to actual
usage; output-gate failures retain known billed usage while transport failures
without usage release the reservation. The built-in transport
enforces HTTPS/exact hosts, input/output/response caps, JSON-object output, and
credential redaction in both prompts and errors. `/api/model-usage` is an
admin-only tenant-scoped operational view, and the complete gateway is
replaceable through `evoagent.model-gateway`.

The v0.10 increment adds a versioned TOML topology whose routes reference
environment-held credentials, exact tenant/repository selectors, region tags,
priority, and pricing. Repository policy evaluates the complete route catalog
at intake and the task snapshot is enforced again for every call. Fallback is
bounded, occurs only for classified eligible failures, and every route owns an
independent breaker. Schema version 7 correlates attempts by root request and
route id.

The v0.18 increment quarantines reservations left by a process crash as
`uncertain`, retains their worst-case budget charge, and permits only a
tenant-scoped administrator to apply provider-verified actual usage. Ledger and
audit update atomically, and SQLite/PostgreSQL share the same expiry, late
completion, tenant isolation, budget, and reconciliation contracts.

The v0.23 increment adds topology v2. Active routes in one priority tier use
deterministic weighted assignment while priority still orders fallback.
Candidate routes run only after a successful active result through a bounded,
lifecycle-managed executor; they cannot enter the production catalog, affect
readiness, or change the returned review. Schema version 11 records
active/shadow usage lanes and content-free observations. Total budgets and
optional shadow-only ceilings are enforced atomically by both database
adapters. A tenant-scoped report gates promotion on sample/error/disagreement
thresholds, no pending observations, and independent evaluation evidence
digests. Activation remains a reviewed topology change and redeploy rather than
an API mutation.

The v0.24 increment adds declarative max-in-flight and requests-per-minute
capacity to topology v2. PostgreSQL atomically coordinates leases and fixed
minute windows across replicas and across rolling topology hashes by stable
route ID; SQLite provides development-equivalent semantics. Capacity rejects
happen before budget/provider use and participate in the existing bounded
fallback budget. Schema version 12 retains content-free lease/counter evidence.
An administrator report combines live counters and breaker state with normalized
weight recommendations derived from declared capacity; GitOps remains the only
mutation path.

Still pending before this item is complete: automatic provider-capacity
discovery/forecasting, automatically applied weights, and a production
evaluation corpus. Multi-route support improves dependency resilience but is
not a claim of regional HA without deployment-level network and capacity proof.

### Remote proof runner — authenticated baseline implemented

- move arbitrary repository execution out of the API/worker trust domain;
- use short-lived container or microVM jobs with no ambient credentials;
- signed input/output manifest, artifact size caps, network-deny default, and
  immutable evidence retention.

Acceptance:

- a compromised repository job cannot reach Store, Redis, GitHub, LLM keys, or
  cloud metadata;
- runner timeout/capacity failure is represented as uncertainty, never proof;
- every proof can be reproduced from a content-addressed evidence bundle.

Implemented evidence: `ProofExecutorPort` and the stable `proof.executor`
capability separate evidence grading from execution placement. The API adapter
uses a canonical, versioned HMAC-SHA256 protocol with exact HTTPS host policy,
no redirects/proxies, replay and byte caps, and verifies a response bound to the
same request/input/evidence digests. `evoagent-proof-runner` is an independent
process that refuses host execution, starts bounded netless containers without
injected environment variables, and can fail closed on append-only
content-addressed input/evidence persistence. Each evidence step returns the
verified attestation; transport/capacity/runner failures remain L1/L2/L3
uncertainty according to the ladder. Unit attack tests cover tampering, expiry,
replay, allowlists, artifact immutability, and failure mapping; mandatory Docker
CI exercises the complete HTTP → signature → runner → netless container path.

The v0.21 increment extracts `ProofReplayStorePort`: local development keeps a
bounded memory adapter, while production replicas atomically claim UUID nonces
through Redis `SET NX` + TTL. Runner `/readyz` fails with the shared dependency,
and an outage returns 503 before execution. Body-bound key IDs plus exactly one
previous verification key provide a documented rolling rotation with no
unsigned/downgrade window; responses use the request-selected key. Real Redis
CI proves cross-adapter atomicity and TTL.

The v0.22 increment extracts `ProofArtifactStorePort` and separates execution
from retention. Local content addressing remains available, while a production
adapter uses an S3 bucket with Versioning and Object Lock, conditional create,
full-object SHA-256, exact version/metadata/mode/date verification, optional KMS,
and non-shortening retention extension. Required storage participates in Runner
readiness and fails before repository execution when the input artifact cannot
be retained. Unit tests cover idempotence, a conflicting/tampered object,
retention extension, bucket readiness, secret-safe settings, and lifecycle; an
explicit environment-gated integration test verifies a real bucket's Object
Lock and Versioning configuration.

Still pending for hostile public multi-tenancy: a microVM executor. Regulated
production additionally requires an independent IAM/bucket-policy review and a
provider-backed write/restore/expiry drill; configuration health alone is not a
compliance certificate. See
[`ADR 0009`](adr/0009-authenticated-remote-proof-runner.md) and
[`ADR 0020`](adr/0020-proof-artifact-object-lock.md).

## Phase 5 — SLO, operations, and disaster recovery

Define and continuously measure:

- intake availability and p95/p99 acceptance latency;
- queue age, task completion latency, provider error rate, and cost per review;
- false-positive/false-negative feedback trends;
- repair abstention/pass/regression rates;
- tenant quota saturation and noisy-neighbor events.

Deliver dashboards, alert rules, runbooks, capacity tests, backup/restore drills,
and a release rollback exercise. A release is enterprise-ready only when these
procedures are repeatable by someone other than the author.

Implemented SLO baseline: HTTP responses are classified into fixed probe/read/
intake/heavy/proof/write families; review duration is a histogram; Queue and
Outbox export oldest work age plus current DLQ depth. `ops/slo.toml` defines the
versioned 30-day contract, `evoagent-slo` evaluates scalar Prometheus queries
through an exact-host HTTPS/no-redirect/no-proxy client, Prometheus rules encode
fast/slow budget burn and dependency backlog alerts, and the packaged Grafana
dashboard links to actionable runbooks. CI validates the rules with `promtool`
and verifies all operational assets in the wheel.

The v0.19 increment adds one correlated HTTP boundary around admission plus GET
and POST execution. Every response carries a strict `X-Request-ID`; unexpected
failures return no exception detail, and structured access/error records omit
query strings and exception messages. Response-start tracking prevents a late
failure from corrupting an already-started HTTP stream. See
[`ADR 0017`](adr/0017-correlated-safe-http-boundary.md).

The v0.20 increment extends that rule behind the HTTP edge. Task/Trace/
Checkpoint, Agent messages, Queue/DLQ, Outbox/effect receipts, readiness,
shadow/evaluation failures, proof/verifier launch failures, plugin lifecycle,
and OpenTelemetry now carry only an allowlisted operation, bounded exception
type, and message-independent code-location reference. Both Store adapters
reject raw operational strings, while migration 10 removes legacy messages from
known persistence fields. See
[`ADR 0018`](adr/0018-message-free-operational-failures.md).

The v0.25 increment closes the locally measurable quality/economics gaps.
Known-billed model tokens and micro-unit cost include successful responses and
post-response contract failures, with separate fixed active/shadow lanes and
latency histograms. Capacity rejections split concurrency/rate causes; repair
attempts terminate as published, abstained, verification-blocked, or failed;
feedback uses four bounded category counters. Recording rules derive cost per
terminal review, capacity saturation, verification-block rate, and negative
feedback trend. Alerts require minimum volumes, dashboards expose each signal,
and runbooks explicitly prevent sparse feedback from being treated as unbiased
accuracy evidence. Tenant/repository/route values never become metric labels.
See [`ADR 0023`](adr/0023-fixed-cardinality-quality-economics.md).

The v0.26 increment closes the reverse-proxy admission identity gap. Forwarded
addresses are default-deny and become authoritative only when the direct socket
peer belongs to one of at most 64 explicit canonical CIDRs. A bounded literal-IP
chain is walked from right to left and stops at the first untrusted hop, so an
attacker cannot prepend a new rate-limit identity. Malformed chains fall back to
the socket peer, raw forwarding values never enter logs or metric labels, and
fixed counters distinguish accepted, ignored, and invalid forwarding metadata.
See [`ADR 0024`](adr/0024-trusted-proxy-client-identity.md).

The v0.27 increment closes the unbounded application-history baseline without
turning retention into an unsafe table TTL. A Store-level transaction prunes
only old terminal-task Trace events and superseded completed session findings,
preserving the latest event, latest completed turn, and any snapshot required
by an out-of-order pending turn. Durable markers distinguish intentional
deletion from empty data. The disabled-by-default coordinator bounds every
batch and run, participates in shutdown, publishes fixed-cardinality metrics,
and is covered by health, dashboard, alert, runbook, and the shared
SQLite/PostgreSQL adapter contract. See
[`ADR 0025`](adr/0025-state-aware-operational-retention.md).

The v0.28 increment closes the durable noisy-neighbor admission baseline. New
REST and webhook reviews atomically reserve a per-tenant database slot with the
task/session/Outbox transaction; PostgreSQL uses a stable tenant advisory lock
to coordinate replicas. Async failures keep ownership through retries and
offline reconstruction, while success, cancellation, and final dead-letter
disposition release it. Resume reacquires capacity and advances a generation so
an older delivery callback cannot free the resumed slot. Disabled-by-default
enforcement still records occupancy; 429/`Retry-After`, tenant-authorized
inspection, audit, fixed-cardinality metrics, dashboard, alert, migration and
runbook complete the operational path. This is a uniform hard bound, not a
claim of weighted-fair Redis scheduling. See
[`ADR 0026`](adr/0026-durable-tenant-review-admission.md).

The v0.29 increment closes the dispatch-start fairness baseline without
replacing Redis Streams durability. A bounded v1 TOML policy gives each tenant
an integer turn weight and canonical SHA-256; publishers attach only a derived
tenant key, weight, and policy digest. Cross-replica Lua decisions atomically
grant a weighted streak or move an entry to the stream tail while another
tenant waits. Separate waiting, entry, and admitted indexes make retries and
`XAUTOCLAIM` recovery single-accounted; a live-delivery heartbeat prevents
long-running handlers from being falsely reclaimed, while legacy unmarked
envelopes remain compatible for a two-stage rollout. Tenant-authorized inspection, fixed metrics,
health, dashboard, churn alert and runbook complete the operator path. The
mechanism allocates starts rather than CPU seconds, model cost, or completion
latency, and currently targets one logical Redis primary rather than sharded
Redis Cluster; production-shaped cost-aware scheduling and cluster-aware key
placement remain pending. See
[`ADR 0027`](adr/0027-weighted-tenant-fair-redis-dispatch.md).

Implemented database recovery baseline: `evoagent-dr` performs SQLite online
backup/restore or a PostgreSQL exported-snapshot `pg_dump` followed by
`pg_restore` into a strictly generated disposable database. A pass requires
checksummed migration history, schema metadata, per-table counts and
bounded-memory content fingerprints, Store read/write smoke, target cleanup,
artifact SHA-256, and explicit RPO/RTO gates. CI runs the real PostgreSQL path
and retains only the non-row-data evidence manifests. See
[`ADR 0011`](adr/0011-isolated-database-recovery-drills.md).

Implemented queue recovery baseline: `evoagent-recover-queue` produces a
payload-hash-only plan for incomplete tasks, including retry-managed `FAILED`
tasks with an active admission, rejects non-empty Redis, binds apply to the
reviewed plan SHA-256, reserves a plan-bound recovery epoch, and transactionally resets or
reconstructs Outbox intent with an idempotent audit record. Missing Diff and
Outbox data fail closed; terminal duplicates ACK before external or release
effects. CI exercises this against PostgreSQL and a fresh Redis logical DB. See
[`ADR 0012`](adr/0012-offline-queue-reconstruction.md).

Still pending in this phase: managed PITR/backup-artifact object-lock integration, a
separate-region infrastructure and routing failover exercise, and a sustained
production-shaped soak. Same-cluster database and queue drills are not a claim
of full regional DR readiness.

## Phase 6 — Independent quality evidence

Implemented governance baseline:

- the public-PR importer emits answer-free, content-addressed inputs with a
  human rights-review record;
- `evoagent-eval-labels` requires at least two blind independent annotations and
  a separate adjudicator, validates labels against added lines, computes
  agreement, and emits reproducible dataset/annotation hashes;
- production provenance checks repository-disjoint splits, holdout coverage,
  unique content, immutable GitHub source, approved usage basis, one annotation
  protocol, and exact sidecar binding;
- reports include per-language, per-CWE, per-rule, ECE, Brier, and invalid
  confidence metrics; baseline and candidate must use the same dataset hash;
- the synthetic corpus remains a separate deterministic regression benchmark.

Still pending: acquire and human-review a legally usable public PR sample,
complete independent annotation/adjudication, and retain the resulting private
raw packets plus public-safe sidecar under the organization's data-retention
policy. `production_data_provenance` remains blocked until that real evidence is
provided; metadata alone cannot bypass it. See
[`ADR 0013`](adr/0013-independent-evaluation-evidence.md).

## Recommended execution order

1. Domain ports + adapter contract tests.
2. Database migrations + transactional outbox.
3. PostgreSQL/Redis/container CI matrix and chaos recovery.
4. `ReviewService` use-case decomposition and repository policy scopes.
5. Model gateway + remote proof runner.
6. SLO/DR operations and independently labeled evaluation.

Every phase must preserve `make check`, backward-compatible API behavior, and a
documented migration/rollback path.
