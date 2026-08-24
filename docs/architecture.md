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
  -> compare pull_request.updated_at with the durable session fence; record and ignore older events
  -> closed/draft PR: end session + cancel unfinished turns + suppress pending comments
  -> reopened/ready_for_review: reopen the same session
  -> transaction: tenant slot + task + diff + outbox
  -> outbox dispatcher
  -> memory queue or Redis Stream
  -> checkpointed review loop
       parse -> plan -> reviewers/skills -> evidence gate -> report
  -> re-read the repository kill switch
  -> optional GitHub comment or verified repair PR
```

PostgreSQL is the source of truth. Redis is replaceable delivery state: a
committed outbox record can republish a missing queue item, and message keys
make the publish/ack crash window idempotent. Workers reject queue metadata that
does not match the persisted task tenant, repository and credential snapshot,
revalidate the installation binding before every queued delivery or repair, and
apply the same bounded exponential retry backoff on both delivery backends.

## Runtime boundaries

- `PostgresTaskStore` owns durable consistency and cross-replica admission.
- Each container runs one process; the deployment platform owns horizontal
  replication and process supervision.
- `TaskQueuePort` separates the in-process and Redis delivery backends.
- `ModelGatewayPort`, `CodeHostPort` and `ProofExecutorPort` are external
  trust boundaries.
- Validated GitHub installation tokens use a bounded process-local LRU and
  refresh once per installation under concurrent demand; a 401 evicts only the
  matching stale token so the next delivery attempt refreshes it.
- GitHub App comment upserts match both the stable marker and configured App bot
  login, so a copied marker in an untrusted PR comment cannot redirect the update.
- Dynamic Skills run out of process and are snapshotted by content hash.
- When Skill signing is enabled, HMAC-SHA256 covers the canonical manifest JSON
  excluding `signature`; the signed `sha256` field binds the verified source snapshot.
- Every reviewer in that snapshot is required; one specialist failure fails and
  retries the task instead of publishing a partial review as complete.
- Task identity and admission generation propagate through reviewer wrappers, so
  composed contextual reviewers keep the same persistence fence as the coordinator.
- The specialist pool shares the task timeout; queued reviewers are cancelled when
  that deadline expires instead of multiplying their individual timeouts. Running
  reviewers retain their bounded coordinator slots until they return, so repeated
  timeouts cannot accumulate replacement threads.
- The Skill root is rejected above 32 top-level entries before manifests or
  source files are loaded.
- Skill identifiers, versions, descriptions, source labels and entrypoints are
  length-bounded; entrypoints are rejected before any filesystem lookup.
- Finding and trusted FixRule identifiers are bounded printable, whitespace-free
  tokens before they enter collaboration storage, repository policy or repair selection.
- Trusted contributions are capped before registration; the complete specialist
  graph is rejected above 64 reviewers before any worker futures are created.
  Reviewer identities are bounded printable tokens at registration and before
  they reach messages or storage.
- Each specialist is capped at 100 findings before collaboration messages or
  critic/test fan-out are persisted.
- Every process loads one immutable Skill set at startup; there is no runtime
  reload endpoint. Startup validates a separate
  candidate registry and reviewer graph before publishing the active snapshot;
  service inventory and execution reads resolve that snapshot directly instead
  of caching mutable aliases.
- New tasks bind the application version and path-independent Python source
  digest, model route and executable reviewer/Skill inventory. Workers compare
  that revision before loading checkpoints, so a retry cannot silently continue
  under different application code or a different reviewer graph.
- Built-in reviewers and fix rules are direct dependencies, not plugins.

The review loop is ordinary Python. Its fixed node order does not require a
workflow framework. Checkpoints provide restartability independently of how
the nodes are invoked.

Replay evaluation runs each Prompt candidate through the same evidence graph as
production, without persisting evaluation collaboration messages, and only
approves an inactive candidate. Tenant-scoped
canary/shadow rollout is the sole path from an approved version to production
traffic; evaluation cannot reload or globally activate it.
Shadow execution is best-effort: its failure is audited but cannot replace a
persisted primary-review success or change the client response.
It runs the same reviewer and evidence graph as primary traffic without writing
shadow collaboration messages into the primary task.
Stable traffic shadows the candidate; overlapping canary traffic shadows the
stable reviewer and records both in baseline-to-candidate order.

## Failure and recovery model

- Every accepted asynchronous task and outbox intent commit together.
- API task acceptance and its authenticated `review.create` audit commit together.
- Checkpoint writes serialize with task cancellation on the task row, so a
  cancellation that wins the race cannot be overwritten by late node output.
- Operator cancellation commits its task state, admission release, trace and
  actor audit in the same PostgreSQL transaction.
- Completed checkpoints are first-write-wins, and failed checkpoints accept
  only nondecreasing attempts, so stale workers cannot degrade recovery state.
- Collaboration, checkpoint, progress and terminal writes also match the
  worker's active admission generation, so a resumed task rejects every late
  prior worker; an older queued generation is ACKed as a duplicate when a newer
  admission is active instead of creating a false DLQ incident.
- A worker whose node callback loses that race reuses the durable completed
  checkpoint instead of continuing with divergent output or retrying a stale failure.
- Success is immutable: late checkpoints, transitions, failures and duplicate
  successes are locked out without replacing the first report or trace.
- Active task progress is monotonic under the same row lock; duplicate or stale
  worker transitions cannot regress state or append misleading Trace events.
- Execution-error learning cases share that lock and are resolved by a
  successful retry; explicit user feedback remains valid after success.
- A shadow observation and its completed/failed operator audit share one
  transaction; notification failure cannot reclassify the durable rollout result.
- An asynchronous worker that loses the success race delivers the persisted
  winning report and ACKs instead of recording failure or retrying.
- Session turns use the existing per-session database lock with first-completion
  semantics, so concurrent result delivery cannot rebuild or recount a turn.
- Task metadata patches use one native PostgreSQL `jsonb` merge, preventing
  concurrent delivery and deferred-diff updates from replacing each other.
- Collaboration messages are inserted only while the task is neither cancelled
  nor successful, preventing late reviewers from extending terminal history.
- Redis handlers heartbeat leases; abandoned work is reclaimed after the lease.
- Exhausted messages enter the DLQ for diagnosis; task resume reacquires
  admission and publishes a fresh generation so stale callbacks cannot release
  a new slot.
- PostgreSQL migrations are forward-only and checksummed.
- Restore drills target a disposable database and compare schema/content
  fingerprints before reporting RPO/RTO evidence.

See [transactional outbox](transactional-outbox.md),
[database migrations](database-migrations.md), and
[disaster recovery](disaster-recovery.md).

## Trust boundaries

PR content, HTTP metadata, model responses and Skill output are untrusted.
GitHub/model hosts are exact allowlists; the single model route is bounded before
TOML parsing, and model input/output budgets are explicit integers;
findings must point to added diff lines; Proof and untrusted repair tests run in
containers without inherited credentials. See [threat model](threat-model.md).
