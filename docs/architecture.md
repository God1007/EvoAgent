# EvoAgent Architecture

This document describes the core components, the data flow of a single review,
and the durability/recovery model. It complements the high-level diagrams in the
[README](../README.md) and the decision records under [`docs/adr/`](adr/).

## 1. Component map

| Layer | Module | Responsibility |
| --- | --- | --- |
| Intake | `evoagent/api.py` | HTTP API, static web console, `/webhooks/github`, `/health`, `/metrics` |
| Composition | `evoagent/plugins.py` | Trusted plugin manifests, capability registry, scopes, events, dependency graph, lifecycle rollback |
| Composition | `evoagent/capabilities.py` | Stable typed capability definitions for providers and consumers |
| Domain boundary | `evoagent/ports.py` | Focused Store, Queue, and CodeHost behavioral contracts |
| Composition | `evoagent/bootstrap.py` | Replaceable built-in provider catalog and transactional application startup |
| Orchestration | `evoagent/service.py` | Consumes composed capabilities, enqueues/processes reviews, GitHub integration |
| Runtime | `evoagent/harness.py` | LangGraph state machine, budget, retry, checkpoint/resume |
| Runtime | `evoagent/task_queue.py` | In-process queue or Redis Streams (ACK, lease, DLQ, replay) |
| Runtime | `evoagent/outbox.py` | Store-to-queue transactional publication, leases, retry and recovery |
| Review | `evoagent/review_engine.py`, `agents.py` | Replaceable review engine and default multi-agent collaboration protocol |
| Review | `evoagent/reviewer.py` | Local deterministic rules + OpenAI-compatible reviewer + composite |
| Review | `evoagent/skills.py` | Dynamic skill registry, manifest/hash/signature checks, sandboxed execution |
| Delivery | `evoagent/fix_rules.py`, `fixer.py` | Pluggable deterministic transforms plus verified auto-repair on a dedicated branch |
| Delivery | `evoagent/verifier.py` | Compile/test gates with container or host isolation |
| Delivery | `evoagent/report.py` | Injection-safe markdown report rendering |
| Evolution | `evoagent/evolution.py`, `rollout.py` | Prompt versioning, validation/holdout replay, canary/shadow, rollback |
| Evolution | `evoagent/evaluation_harness.py`, `evaluation_benchmark.py` | Replay scoring, benchmark dataset |
| Storage | `evoagent/store.py`, `postgres_store.py` | Task/finding/feedback/version/audit persistence |
| Storage | `evoagent/migrations.py` | Locked, checksummed SQLite/PostgreSQL schema history and compatibility gate |
| Cross-cutting | `evoagent/auth.py` | JWT, RBAC, tenant isolation |
| Cross-cutting | `evoagent/observability.py`, `metrics.py` | Trace, Prometheus metrics, OpenTelemetry |
| Model | `evoagent/models.py` | `Finding`, `Severity`, `ReviewReport`, stable fingerprints |
| Adapters | `evoagent/github.py`, `diff_parser.py` | Hardened GitHub client, unified-diff parsing |

## 2. Review data flow

```text
process startup
  → load built-ins + allowlisted trusted plugins
  → validate manifest/API/dependencies/cycles
  → activate providers in dependency order
  → resolve ReviewService capabilities

change (webhook | REST | console)
  → ReviewService.enqueue_review            # atomically persist task + outbox
  → OutboxDispatcher                        # lease + idempotent publication
  → TaskQueue                               # in-process or Redis Streams
  → ReviewHarness.run                       # LangGraph nodes, checkpoint each step
      parse → plan → specialists (security, reliability, llm, skills)
            → evidence gate (critic, test) → synthesize → verify
  → ReviewReport (findings with stable fingerprints)
  → delivery: report + optional PR comment upsert + optional verified fix PR
```

Each node persists a checkpoint. When a worker restarts mid-task, `resume`
continues from the last completed node instead of replaying the whole graph.

## 3. Composition and lifecycle

```text
PluginProfile
  → provider catalog
  → dependency graph validation
  → topological activation
  → CapabilityRegistry
  → ReviewService consumers
```

Capability registration, event subscription, and resource ownership are
reversible effects. A startup error rolls back the complete candidate graph in
reverse order. Service shutdown first drains/closes the task queue and then
unwinds the plugin graph, so consumers stop before their dependencies.
The queue stops accepting submissions and waits up to
`EVOAGENT_QUEUE_SHUTDOWN_TIMEOUT_SECONDS` for active/already-scheduled work;
timeouts increment `queue_drain_timeouts_total` and emit `queue.drain-timeout`
before the remaining infrastructure is forced closed.

The default graph exposes stable capabilities for settings, store,
observability, GitHub/LLM circuit breakers, GitHub delivery, the review engine,
repair rules, the verified fixer, authentication, release governance, alerting,
evolution, and the queue factory. `ReviewService` consumes these contracts and
does not instantiate their implementations directly.

Capability keys select providers; domain Ports constrain provider behavior.
Lower-level modules depend on focused Store facets rather than the complete
database adapter. Shared backend behavior tests are documented in
[`adapter-contracts.md`](adapter-contracts.md).

Trusted plugins and Dynamic Skills are separate trust levels:

- Trusted plugins run in-process, can own infrastructure resources, and require
  explicit operator installation and allowlisting.
- Dynamic Skills are untrusted review extensions and remain in the restricted
  subprocess/container path without host credentials.

See [`plugin-system.md`](plugin-system.md) and
[`ADR 0002`](adr/0002-trusted-plugin-microkernel.md).

## 4. Durability and recovery

- **Task store** (`store.py` / `postgres_store.py`) is the source of truth for
  task state, findings, feedback, skill versions, audit log, and alerts.
- **Schema history** is forward-only and checksummed. Startup serializes pending
  migrations and refuses a database created by a newer application. See
  [`database-migrations.md`](database-migrations.md) and
  [`ADR 0003`](adr/0003-versioned-forward-only-migrations.md).
- **Queue durability** depends on the backend. `redis-streams` provides ACK,
  consumer leases, dead-letter queue, and replay. The in-process
  `memory-ephemeral` backend is **not** durable (`/health` reports
  `queue_durable: false`) and is for single-process development only.
  Redis workers reconnect after transient transport failures; acknowledged
  entries are atomically ACKed and deleted so memory and operational depth do
  not grow forever. `/ready` checks both Redis reachability and worker liveness.
- **Durable acceptance** uses a transactional outbox. A committed task and its
  queue intent cannot diverge; dispatch leases and message-key dedupe recover
  the publish/ack crash window. See
  [`transactional-outbox.md`](transactional-outbox.md) and
  [`ADR 0004`](adr/0004-transactional-outbox.md).
- **Idempotency**: GitHub webhook delivery id, payload digest, and PR update
  time prevent invalid replay; comments use stable upsert markers and repair PRs
  use effect receipts plus deterministic branches.

## 5. Trust boundaries (summary)

The full analysis lives in [`threat-model.md`](threat-model.md). Key boundaries:

- **PR content is untrusted input** — never treated as instructions.
- **Untrusted code execution** only happens in the verifier, and only when a
  test command is configured (container isolation recommended; host fallback is
  for trusted repositories only).
- **Outbound GitHub requests** are restricted to an HTTPS host allowlist with
  redirect token-stripping and response-size caps.
- **Dynamic skills** run in a restricted subprocess with no host credentials.
- **Trusted plugins** run in the main process and must be pinned, reviewed, and
  explicitly allowlisted. Plugin Scope is not a sandbox.

## 6. Extension points

- **New detection rule**: add to `LocalRuleReviewer.RULES` or ship a sandboxed Dynamic Skill.
- **New reviewer/model**: implement the `Reviewer` interface; compose via
  `CompositeReviewer`.
- **New skill**: drop a manifest + entrypoint under `skills/<name>/`; see the
  README "自定义 Skill" section.
- **New deterministic repair**: implement `FixRule` and provide the multi-valued
  `FIX_RULE` capability; `SafeFixer` retains verification and publication gates.
- **New storage/queue/code-host/workflow backend**: implement a trusted provider
  for the stable key in `evoagent.capabilities`, satisfy its Protocol in
  `evoagent.ports`, pass the shared adapter contracts, and declare dependencies
  in its manifest.
- **New lifecycle observer**: subscribe to sanitized events such as
  `review.completed`; observer failures are isolated from the review path.

## 7. Deliberate boundaries

The microkernel does not make task success, authorization, tenant isolation,
checkpoint semantics, or verification proof arbitrary middleware. These are
domain invariants. Live global hot-swap is also not supported yet: deployments
build and validate a fresh process graph, then rely on orchestrator rollout and
draining. This avoids claiming zero-downtime replacement before in-flight task
handover semantics exist.
