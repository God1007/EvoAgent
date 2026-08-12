# EvoAgent Architecture

This document describes the core components, the data flow of a single review,
and the durability/recovery model. It complements the high-level diagrams in the
[README](../README.md) and the decision records under [`docs/adr/`](adr/).

## 1. Component map

| Layer | Module | Responsibility |
| --- | --- | --- |
| Intake | `evoagent/api.py` | HTTP API, static web console, `/webhooks/github`, `/health`, `/metrics` |
| Orchestration | `evoagent/service.py` | Wires components together, enqueues/processes reviews, GitHub integration |
| Runtime | `evoagent/harness.py` | LangGraph state machine, budget, retry, checkpoint/resume |
| Runtime | `evoagent/task_queue.py` | In-process queue or Redis Streams (ACK, lease, DLQ, replay) |
| Review | `evoagent/agents.py` | Multi-agent collaboration protocol (Planner … Verifier) |
| Review | `evoagent/reviewer.py` | Local deterministic rules + OpenAI-compatible reviewer + composite |
| Review | `evoagent/skills.py` | Dynamic skill registry, manifest/hash/signature checks, sandboxed execution |
| Delivery | `evoagent/fixer.py` | Conservative, verified auto-repair on a dedicated branch |
| Delivery | `evoagent/verifier.py` | Compile/test gates with container or host isolation |
| Delivery | `evoagent/report.py` | Injection-safe markdown report rendering |
| Evolution | `evoagent/evolution.py`, `rollout.py` | Prompt versioning, validation/holdout replay, canary/shadow, rollback |
| Evolution | `evoagent/evaluation_harness.py`, `evaluation_benchmark.py` | Replay scoring, benchmark dataset |
| Storage | `evoagent/store.py`, `postgres_store.py` | Task/finding/feedback/version/audit persistence |
| Cross-cutting | `evoagent/auth.py` | JWT, RBAC, tenant isolation |
| Cross-cutting | `evoagent/observability.py`, `metrics.py` | Trace, Prometheus metrics, OpenTelemetry |
| Model | `evoagent/models.py` | `Finding`, `Severity`, `ReviewReport`, stable fingerprints |
| Adapters | `evoagent/github.py`, `diff_parser.py` | Hardened GitHub client, unified-diff parsing |

## 2. Review data flow

```text
change (webhook | REST | console)
  → ReviewService.enqueue_review            # persist task, enqueue
  → TaskQueue                               # in-process or Redis Streams
  → ReviewHarness.run                       # LangGraph nodes, checkpoint each step
      parse → plan → specialists (security, reliability, llm, skills)
            → evidence gate (critic, test) → synthesize → verify
  → ReviewReport (findings with stable fingerprints)
  → delivery: report + optional PR comment upsert + optional verified fix PR
```

Each node persists a checkpoint. When a worker restarts mid-task, `resume`
continues from the last completed node instead of replaying the whole graph.

## 3. Durability and recovery

- **Task store** (`store.py` / `postgres_store.py`) is the source of truth for
  task state, findings, feedback, skill versions, audit log, and alerts.
- **Queue durability** depends on the backend. `redis-streams` provides ACK,
  consumer leases, dead-letter queue, and replay. The in-process
  `memory-ephemeral` backend is **not** durable (`/health` reports
  `queue_durable: false`) and is for single-process development only.
- **Idempotency**: GitHub webhook delivery id, payload digest, and PR update
  time are checked to prevent duplicate consumption and replay outside the
  allowed window.

## 4. Trust boundaries (summary)

The full analysis lives in [`threat-model.md`](threat-model.md). Key boundaries:

- **PR content is untrusted input** — never treated as instructions.
- **Untrusted code execution** only happens in the verifier, and only when a
  test command is configured (container isolation recommended; host fallback is
  for trusted repositories only).
- **Outbound GitHub requests** are restricted to an HTTPS host allowlist with
  redirect token-stripping and response-size caps.
- **Dynamic skills** run in a restricted subprocess with no host credentials.

## 5. Extension points

- **New rule**: add to `LocalRuleReviewer.RULES` in `reviewer.py`.
- **New reviewer/model**: implement the `Reviewer` interface; compose via
  `CompositeReviewer`.
- **New skill**: drop a manifest + entrypoint under `skills/<name>/`; see the
  README "自定义 Skill" section.
- **New storage backend**: implement the `TaskStore` surface used by
  `service.py`.
