# EvoAgent Architecture

This document describes the core components, the data flow of a single review,
and the durability/recovery model. It complements the high-level diagrams in the
[README](../README.md) and the decision records under [`docs/adr/`](adr/).

## 1. Component map

| Layer | Module | Responsibility |
| --- | --- | --- |
| Intake | `evoagent/api.py`, `errors.py` | HTTP API, static web console, bounded query admission, explicit client-safe errors, correlated 5xx boundary, `/webhooks/github`, `/health`, `/metrics` |
| Composition | `evoagent/plugins.py` | Trusted plugin manifests, capability registry, scopes, events, dependency graph, lifecycle rollback |
| Composition | `evoagent/capabilities.py` | Stable typed capability definitions for providers and consumers |
| Domain boundary | `evoagent/ports.py` | Focused Store, Queue, CodeHost, Model Gateway, and Proof Executor behavioral contracts |
| Composition | `evoagent/bootstrap.py` | Replaceable built-in provider catalog and transactional application startup |
| Application | `evoagent/application/` | Focused Review, Webhook, Session, Repair, Policy, and Model Usage use cases |
| Composition facade | `evoagent/service.py` | Capability wiring, lifecycle/health, canary/shadow runtime selection, API compatibility |
| Runtime | `evoagent/harness.py` | LangGraph state machine, budget, retry, checkpoint/resume |
| Runtime | `evoagent/task_queue.py` | In-process queue or Redis Streams (ACK, lease, DLQ, replay) |
| Runtime | `evoagent/outbox.py` | Store-to-queue transactional publication, leases, retry and recovery |
| Review | `evoagent/review_extensions.py`, `review_engine.py`, `agents.py` | Stable reviewer-contribution seam, replaceable engine, and default multi-agent collaboration protocol |
| Review | `evoagent/reviewer.py` | Local deterministic rules + governed gateway reviewer + composite |
| Model gateway | `evoagent/model_gateway.py` | Secret redaction, egress/output limits, budget reservation, crash quarantine, and usage accounting |
| Review | `evoagent/skills.py`, `skill_runner.py` | Transactional dynamic-skill snapshots, manifest/hash/signature checks, bounded sandboxed execution |
| Delivery | `evoagent/fix_rules.py`, `fixer.py` | Pluggable deterministic transforms plus verified auto-repair on a dedicated branch |
| Delivery | `evoagent/verifier.py` | Compile/test gates with container or host isolation |
| Evidence | `evoagent/proof.py`, `proof_remote.py` | L1–L4 grading plus authenticated local/remote execution and attestations |
| Delivery | `evoagent/report.py` | Injection-safe markdown report rendering |
| Evolution | `evoagent/evolution.py`, `rollout.py` | Prompt versioning, validation/holdout replay, canary/shadow, rollback |
| Evolution | `evoagent/evaluation_harness.py`, `evaluation_dataset.py`, `evaluation_provenance.py`, `evaluation_metrics.py`, `evaluation_labels.py`, `evaluation_benchmark.py` | Blind-label compilation, evidence audit, replay orchestration, replaceable slices, and calibration |
| Storage | `evoagent/store.py`, `postgres_store.py` | Task/finding/feedback/version/audit persistence |
| Policy | `evoagent/policy.py` | Versioned tenant/repository execution and publication decisions |
| Storage | `evoagent/migrations.py` | Locked, checksummed SQLite/PostgreSQL schema history and compatibility gate |
| Cross-cutting | `evoagent/auth.py` | JWT, RBAC, tenant isolation |
| Cross-cutting | `evoagent/observability.py`, `metrics.py` | Trace, Prometheus metrics, OpenTelemetry |
| Operations | `evoagent/slo.py`, `ops/` | Versioned SLO evaluation, Prometheus alerts, and Grafana dashboard |
| Operations | `evoagent/dr.py`, `recovery.py` | Isolated database restore evidence and offline PostgreSQL-to-Redis task reconstruction |
| Model | `evoagent/models.py` | `Finding`, `Severity`, `ReviewReport`, stable fingerprints |
| Adapters | `evoagent/github.py`, `diff_parser.py` | Hardened GitHub client, unified-diff parsing |

Evaluation is a separate evidence boundary: acquisition contains no answers,
annotation is blind and independent, adjudication is a different role, and only
the compiler sidecar can bind labels to a production gate. See
[`ADR 0013`](adr/0013-independent-evaluation-evidence.md).

## 2. Review data flow

```text
process startup
  → load built-ins + allowlisted trusted plugins
  → validate manifest/API/dependencies/cycles
  → activate providers in dependency order
  → resolve ReviewService capabilities

change (webhook | REST | console)
  → HTTP edge (validate/generate request id → admission → safe error envelope)
  → WebhookUseCases | ReviewUseCases
      webhook: delivery + session turn + task + outbox in one transaction
      REST: task + Diff + outbox in one transaction
  → OutboxDispatcher                        # lease + idempotent publication
  → TaskQueue                               # in-process or Redis Streams
  → ReviewHarness.run                       # LangGraph nodes, checkpoint each step
      parse → plan → specialists (security, reliability, llm, skills)
            llm → ModelGateway (scope → route → redact → reserve → call/fallback → validate → account)
            → evidence gate (critic, test) → synthesize → verify
  → ReviewReport (findings with stable fingerprints)
  → delivery: report + optional PR comment upsert + optional verified fix PR
```

Each node persists a checkpoint. When a worker restarts mid-task, `resume`
continues from the last completed node instead of replaying the whole graph.

## 3. Composition and lifecycle

```text
ordered PluginProfile layers
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
Profile composition is bounded and deterministic: base, region, and environment
layers apply in order, plugin configuration uses whole-value replacement, and
the runtime inventory exposes source-content and effective-profile hashes
without configuration values. Configuration is frozen before provider startup.
The queue stops accepting submissions and waits up to
`EVOAGENT_QUEUE_SHUTDOWN_TIMEOUT_SECONDS` for active/already-scheduled work;
timeouts increment `queue_drain_timeouts_total` and emit `queue.drain-timeout`
before the remaining infrastructure is forced closed.

The default graph exposes stable capabilities for settings, store,
observability, GitHub/LLM circuit breakers, GitHub delivery, the review engine,
the model gateway, proof executor, repair rules, the verified fixer, authentication, release
governance, alerting, evolution, and the queue factory. `ReviewService` consumes
these contracts and does not instantiate their implementations directly.

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
- **Model usage recovery** changes expired in-flight reservations to
  `uncertain` while retaining their worst-case budget charge. A tenant-scoped
  administrator can replace that charge only with provider-verified actual
  usage; reconciliation and its audit record commit in the same transaction.
- **Schema history** is forward-only and checksummed. Startup serializes pending
  migrations and refuses a database created by a newer application. See
  [`database-migrations.md`](database-migrations.md) and
  [`ADR 0003`](adr/0003-versioned-forward-only-migrations.md).
- **Recovery proof** uses SQLite online backup or a PostgreSQL exported MVCC
  snapshot plus `pg_dump`, always restores into a disposable target, compares
  schema/content fingerprints, exercises the Store adapter, and emits versioned
  RPO/RTO evidence. See [`disaster-recovery.md`](disaster-recovery.md) and
  [`ADR 0011`](adr/0011-isolated-database-recovery-drills.md).
- **Regional queue reconstruction** treats PostgreSQL task/Outbox records as
  durable intent and Redis Streams as replaceable delivery state. An offline,
  audited recovery epoch stages only incomplete tasks for controlled publication
  into a verified empty Redis database. See
  [`ADR 0012`](adr/0012-offline-queue-reconstruction.md).
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
- **Atomic webhook intake** binds delivery id + payload digest, PR session turn,
  task, and outbox in one Store transaction. Concurrent duplicate deliveries
  return the same task, while injected failures leave no partial records. See
  [`ADR 0006`](adr/0006-use-case-boundaries-and-atomic-webhook-intake.md).
- **Idempotent external effects**: comments use stable upsert markers and repair
  PRs use effect receipts plus deterministic branches.
- **Repository governance** is resolved through a replaceable capability. The
  accepted task stores a versioned policy snapshot while current disable/comment
  kill switches are checked again at execution. See
  [`repository-policies.md`](repository-policies.md) and
  [`ADR 0005`](adr/0005-versioned-repository-policy.md).

## 5. Trust boundaries (summary)

The full analysis lives in [`threat-model.md`](threat-model.md). Key boundaries:

- **PR content is untrusted input** — never treated as instructions.
- **HTTP metadata and failures are untrusted** — request IDs are correlation
  labels only; access logs omit query strings and unexpected 5xx responses never
  expose exception messages.
- **Untrusted code execution** only happens in the verifier, and only when a
  test command is configured (container isolation recommended; host fallback is
  for trusted repositories only).
- **Production proof execution** can cross an authenticated remote boundary.
  The response is bound to request/input/evidence hashes; the remote process
  starts container-only jobs with no application credentials or network.
- **Outbound GitHub requests** are restricted to an HTTPS host allowlist with
  redirect token-stripping and response-size caps.
- **Outbound model requests** pass through a tenant/repository-aware gateway;
  likely credentials are redacted, the configured DNS host and HTTPS scheme are
  checked, responses are size/token bounded, and raw prompts/responses are not
  persisted in the usage ledger.
- **Dynamic skills** run in a restricted subprocess with no host credentials.
- **Trusted plugins** run in the main process and must be pinned, reviewed, and
  explicitly allowlisted. Plugin Scope is not a sandbox.

## 6. Extension points

- **New detection rule**: add to `LocalRuleReviewer.RULES` or ship a sandboxed Dynamic Skill.
- **New reviewer/model route**: replace the `model.gateway` capability or use
  `GatewayReviewer`; model credentials stay outside reviewer/domain objects.
- **New skill**: drop a manifest + entrypoint under `skills/<name>/`; see the
  README "自定义 Skill" section.
- **New deterministic repair**: implement `FixRule` and provide the multi-valued
  `FIX_RULE` capability; `SafeFixer` retains verification and publication gates.
- **New proof runtime**: implement `ProofExecutorPort` and replace the
  `evoagent.proof-executor` plugin/`proof.executor` capability; the evidence
  ladder remains centralized and infrastructure errors remain uncertainty.
- **New storage/queue/code-host/workflow backend**: implement a trusted provider
  for the stable key in `evoagent.capabilities`, satisfy its Protocol in
  `evoagent.ports`, pass the shared adapter contracts, and declare dependencies
  in its manifest.
- **New lifecycle observer**: subscribe to sanitized events such as
  `review.completed`; observer failures are isolated from the review path.
- **New application operation**: add a focused object under
  `evoagent.application`, declare only the Ports it consumes, test it directly,
  and expose a compatibility delegate from `ReviewService` when required.

## 7. Deliberate boundaries

The microkernel does not make task success, authorization, tenant isolation,
checkpoint semantics, or verification proof arbitrary middleware. These are
domain invariants. Live global hot-swap is also not supported yet: deployments
build and validate a fresh process graph, then rely on orchestrator rollout and
draining. This avoids claiming zero-downtime replacement before in-flight task
handover semantics exist.
