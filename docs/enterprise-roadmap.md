# EvoAgent Enterprise Engineering Roadmap

This roadmap separates capabilities already proven in the repository from work
that is still required before claiming production-grade enterprise readiness.
It is an execution plan, not a marketing checklist.

## Current maturity (v0.11.0)

| Area | Status | Evidence / boundary |
| --- | --- | --- |
| Reproducible engineering | Implemented | Hash locks, Ruff, mypy, tests, coverage gate, package build, dependency audit |
| Application composition | Implemented | Trusted plugin graph, typed capabilities, Profile, Scope, rollback, reverse shutdown |
| Application boundaries | Implemented baseline | Focused Review/Webhook/Session/Repair/Policy use cases behind a compatible facade; evolution is an independent capability |
| Review extensibility | Implemented | Reviewer interface, sandboxed Dynamic Skills, replaceable `review.engine` |
| Repair extensibility | Implemented | Independent `fix.rule` providers; verifier and publication gates remain centralized |
| Local durability | Development only | SQLite + memory queue is explicitly non-durable |
| Production persistence | Implemented baseline | PostgreSQL + Redis Streams, migration CLI, Outbox, ACK/lease/DLQ, and mandatory real-service CI; backup/restore drill remains pending |
| Graceful lifecycle | Implemented | Readiness drain plus bounded queue drain before Store/plugin shutdown |
| Multi-tenancy/governance | Implemented baseline | JWT, RBAC, tenant/repository authorization, audit, canary/shadow/rollback |
| Model governance | Implemented advanced baseline | Replaceable gateway, scoped policy routing/residency, bounded fallback, per-route breakers, redaction, egress/output limits, atomic budgets, correlated metadata-only ledger; route shadow promotion remains pending |
| Strong untrusted execution | Implemented baseline | Replaceable remote Proof Runner, mutually authenticated evidence manifests, container-only jobs, replay/size/capacity gates, and content-addressed artifacts; microVM and distributed replay store remain pending |
| Quality evidence | Synthetic benchmark only | 100-case controlled corpus; production activation gate remains blocked without independent labels |
| HA/DR operational proof | Pending | No documented backup/restore drill, regional failover, or sustained production SLO evidence |

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

### 2.2 Versioned database migrations — implemented, restore drill pending

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
`/ready` exposes the schema version. The actual PostgreSQL backup/restore drill
remains part of the integration/operations phase because it requires dedicated
external infrastructure.

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
tests; drives the GitHub client over a real local HTTP fixture; executes the
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

Still pending before this item is complete: candidate-route shadow/promotion
gates, weighted load balancing, and reconciliation of reservations left by a
process crash. Multi-route support improves dependency resilience but is not a
claim of regional HA without deployment-level network and capacity proof.

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

Still pending for hostile public multi-tenancy: a microVM executor, shared
multi-replica replay/nonce storage, dual-key rotation, and object-lock/WORM
retention. See [`ADR 0009`](adr/0009-authenticated-remote-proof-runner.md).

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

## Phase 6 — Independent quality evidence

- import a legally usable public PR dataset with independent labels;
- keep repository-disjoint validation/holdout splits;
- blind-review a sample with at least two annotators and record agreement;
- report per-language, per-rule, and confidence-calibration metrics;
- preserve the synthetic corpus for deterministic regression, but never combine
  its metrics with production observations.

The existing `production_data_provenance` gate stays blocked until this phase is
complete.

## Recommended execution order

1. Domain ports + adapter contract tests.
2. Database migrations + transactional outbox.
3. PostgreSQL/Redis/container CI matrix and chaos recovery.
4. `ReviewService` use-case decomposition and repository policy scopes.
5. Model gateway + remote proof runner.
6. SLO/DR operations and independently labeled evaluation.

Every phase must preserve `make check`, backward-compatible API behavior, and a
documented migration/rollback path.
