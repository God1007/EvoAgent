# Architecture

EvoAgent is a PostgreSQL-backed review service with one optional Redis queue
and one optional model route. `bootstrap.py` composes concrete components
directly; extension points exist only where the repository has more than one
real implementation.

## Components

| Area | Modules | Responsibility |
| --- | --- | --- |
| HTTP | `api.py`, `auth.py`, `backpressure.py` | REST, GitHub webhooks, JWT/RBAC and bounded admission |
| Use cases | `application/`, `service.py` | Review, webhook, session, policy and repair workflows |
| Review | `review_engine.py`, `agents.py`, `reviewer.py` | Plan, run reviewers, gate evidence and synthesize findings |
| Extensions | `review_extensions.py`, `fix_rules.py`, `skills.py` | Reviewer/fix-rule seams and sandboxed dynamic Skills |
| Model | `model_gateway.py` | One redacted, allowlisted OpenAI-compatible route |
| State | `postgres_store.py`, `migrations.py` | Tasks, checkpoints, audit, policy, outbox and tenant admission |
| Delivery | `task_queue.py`, `outbox.py` | Memory or Redis Streams; ACK, lease, retry, dedupe and DLQ |
| Proof/fix | `proof.py`, `verifier.py`, `fixer.py` | Local container proof and verified deterministic repairs |
| Operations | `metrics.py`, `observability.py`, `dr.py`, `recovery.py` | Metrics/traces, PostgreSQL restore drills and queue reconstruction |

## Review flow

```text
REST or GitHub webhook
  -> transaction: tenant slot + task + diff + outbox
  -> outbox dispatcher
  -> memory queue or Redis Stream
  -> checkpointed review loop
       parse -> plan -> reviewers/skills -> evidence gate -> report
  -> optional GitHub comment or verified repair PR
```

PostgreSQL is the source of truth. Redis is replaceable delivery state: a
committed outbox record can republish a missing queue item, and message keys
make the publish/ack crash window idempotent.

## Runtime boundaries

- `PostgresTaskStore` owns durable consistency and cross-replica admission.
- `TaskQueuePort` separates the in-process and Redis delivery backends.
- `ModelGatewayPort`, `CodeHostPort` and `ProofExecutorPort` are external
  trust boundaries.
- Dynamic Skills run out of process and are snapshotted by content hash.
- Built-in reviewers and fix rules are direct dependencies, not plugins.

The review loop is ordinary Python. Its fixed node order does not require a
workflow framework. Checkpoints provide restartability independently of how
the nodes are invoked.

## Failure and recovery model

- Every accepted asynchronous task and outbox intent commit together.
- Redis handlers heartbeat leases; abandoned work is reclaimed after the lease.
- Exhausted messages enter the DLQ and can be replayed with the original key.
- Resume advances the task admission generation so stale callbacks cannot
  release a new slot.
- PostgreSQL migrations are forward-only and checksummed.
- Restore drills target a disposable database and compare schema/content
  fingerprints before reporting RPO/RTO evidence.

See [transactional outbox](transactional-outbox.md),
[database migrations](database-migrations.md), and
[disaster recovery](disaster-recovery.md).

## Trust boundaries

PR content, HTTP metadata, model responses and Skill output are untrusted.
GitHub/model hosts are exact allowlists; model input is redacted and bounded;
findings must point to added diff lines; Proof and untrusted repair tests run in
containers without inherited credentials. See [threat model](threat-model.md).
