# EvoAgent Enterprise Engineering Roadmap

This roadmap separates capabilities already proven in the repository from work
that is still required before claiming production-grade enterprise readiness.
It is an execution plan, not a marketing checklist.

## Current maturity (v0.6.0)

| Area | Status | Evidence / boundary |
| --- | --- | --- |
| Reproducible engineering | Implemented | Hash locks, Ruff, mypy, tests, coverage gate, package build, dependency audit |
| Application composition | Implemented | Trusted plugin graph, typed capabilities, Profile, Scope, rollback, reverse shutdown |
| Review extensibility | Implemented | Reviewer interface, sandboxed Dynamic Skills, replaceable `review.engine` |
| Repair extensibility | Implemented | Independent `fix.rule` providers; verifier and publication gates remain centralized |
| Local durability | Development only | SQLite + memory queue is explicitly non-durable |
| Production persistence | Implemented, integration evidence incomplete | PostgreSQL + Redis Streams, ACK/lease/DLQ; needs mandatory CI matrix and migration tooling |
| Graceful lifecycle | Implemented | Readiness drain plus bounded queue drain before Store/plugin shutdown |
| Multi-tenancy/governance | Implemented baseline | JWT, RBAC, tenant/repository authorization, audit, canary/shadow/rollback |
| Strong untrusted execution | Partial | Container isolation exists; host fallback is not a strong boundary; microVM/remote runner pending |
| Quality evidence | Synthetic benchmark only | 100-case controlled corpus; production activation gate remains blocked without independent labels |
| HA/DR operational proof | Pending | No documented backup/restore drill, regional failover, or sustained production SLO evidence |

## Phase 2 — Ports, persistence, and integration proof

### 2.1 Explicit domain ports

Replace remaining structural/`Any` boundaries with focused Protocols:

- `TaskExecutionStore`, `GovernanceStore`, `SessionStore`, `EvaluationStore`;
- `TaskQueuePort`, `CodeHostPort`, `ModelGatewayPort`;
- contract tests that every SQLite/PostgreSQL and Memory/Redis implementation must pass.

Acceptance:

- application and domain modules depend on ports, not concrete adapters;
- mypy checks both production adapter implementations against the same contracts;
- a fake adapter can run service use-case tests without monkey-patching internals.

### 2.2 Versioned database migrations

Replace constructor-time ad-hoc schema mutation with an explicit migration
history and startup compatibility check.

Acceptance:

- forward migration from the last supported release is tested;
- startup refuses a newer unsupported schema instead of guessing;
- migration rollback/restore procedure is documented;
- PostgreSQL backup and restore is exercised in CI or a scheduled environment.

### 2.3 Transactional task/outbox semantics

Close the failure window between writing a task record and publishing a queue
message. Introduce an outbox with idempotent dispatcher and consumer keys.

Acceptance:

- process death after task commit but before queue publish cannot orphan work;
- duplicate delivery cannot publish duplicate comments or fix PRs;
- chaos tests kill workers at each persistence/ACK boundary and prove recovery.

### 2.4 Mandatory adapter integration matrix

Add CI jobs for PostgreSQL, Redis Streams, GitHub HTTP fixtures, and containerized
Verifier execution. Remove those modules from coverage exclusions only after the
boundary suites exist.

Acceptance:

- Redis lease reclaim and DLQ replay are tested across process restart;
- PostgreSQL pool exhaustion and reconnect behavior are tested;
- wheel-installed resources, health/readiness, and shutdown drain run in the
  built container image.

## Phase 3 — Application decomposition and policy scopes

Split the large `ReviewService` into use-case services while keeping one public
facade for API compatibility:

```text
ReviewApplication
  ├── ReviewUseCases
  ├── PullRequestWebhookUseCases
  ├── RepairUseCases
  ├── SessionUseCases
  └── EvolutionUseCases
```

Introduce tenant/repository policy resolution on top of child plugin scopes:

- allowed reviewers and FixRules;
- LLM/model/data-residency policy;
- token, concurrency, and cost budgets;
- auto-fix and publication approval rules.

Acceptance:

- each use case has a narrow port set and independent tests;
- repository policy changes are audited and versioned;
- one tenant cannot consume another tenant's provider, quota, or event payload.

## Phase 4 — Model gateway and execution isolation

### Model gateway

- provider routing, fallback, retry budget, and per-model circuit breakers;
- request/token/cost accounting by tenant and repository;
- structured-output validation, redaction, data-retention policy, and egress allowlist;
- offline/shadow evaluation before route changes.

### Remote proof runner

- move arbitrary repository execution out of the API/worker trust domain;
- use short-lived container or microVM jobs with no ambient credentials;
- signed input/output manifest, artifact size caps, network-deny default, and
  immutable evidence retention.

Acceptance:

- a compromised repository job cannot reach Store, Redis, GitHub, LLM keys, or
  cloud metadata;
- runner timeout/capacity failure is represented as uncertainty, never proof;
- every proof can be reproduced from a content-addressed evidence bundle.

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
