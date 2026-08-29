# Architecture

EvoAgent is a PostgreSQL-backed review service with one optional Redis queue
and one optional model route. `bootstrap.py` composes concrete components
directly; trusted agent implementations and explicit dataflow wiring can be
supplied at that composition root without dynamic plugin discovery.

## Components

| Area | Modules | Responsibility |
| --- | --- | --- |
| HTTP | `api.py`, `auth.py`, `backpressure.py` | REST, GitHub webhooks, JWT/RBAC and bounded admission |
| Use cases | `application/`, `service.py` | Review, webhook, session, policy and repair workflows |
| Review | `review_engine.py`, `agents.py`, `reviewer.py` | Plan, run reviewers, gate evidence and synthesize findings |
| Review context | `models.py`, `application/reviews.py`, `application/webhooks.py` | Bound and pin PR title, Spec and repository standards as an untrusted typed input |
| Repository evidence | `codegraph.py`, `application/reviews.py` | Conditionally index a head-SHA GitHub archive into a bounded, source-free Python impact snapshot |
| Agent handoff | `workflow.py` | Versioned ports, bounded DAG, isolated inputs and durable output handoffs |
| Extensions | `review_extensions.py`, `fix_rules.py`, `skills.py` | Reviewer/fix-rule seams and sandboxed dynamic Skills |
| Model | `model_gateway.py` | One redacted, allowlisted OpenAI-compatible route |
| State | `postgres_store.py`, `migrations.py` | Tasks, checkpoints, audit, policy, outbox and tenant admission |
| Delivery | `task_queue.py`, `outbox.py` | Memory or Redis Streams; ACK, lease, retry, dedupe and DLQ |
| Proof/fix | `proof.py`, `proof_service.py`, `verifier.py`, `fixer.py` | Container Proof through a local or private-socket executor; separately gated deterministic repairs |
| Sandbox lifecycle | `container_runtime.py` | Shared hardening, independent container lifetime and exact-name cleanup for Skills and verification |
| Operations | `metrics.py`, `observability.py`, `dr.py`, `recovery.py` | Bounded workflow/Agent/model metrics and traces, PostgreSQL restore drills and queue reconstruction |

## Review flow

```text
REST or GitHub webhook
  -> compare pull_request.updated_at with the durable session fence; record and ignore older events
  -> closed/draft PR: end session + cancel unfinished turns + suppress pending comments
  -> reopened/ready_for_review: reopen the same session
  -> transaction: tenant slot + task + input snapshot + outbox
  -> outbox dispatcher
  -> memory queue or Redis Stream
  -> GitHub task: fetch the immutable base_sha...head_sha comparison and persist its Diff/hash
  -> if the pinned workflow wires repository evidence: fetch the pinned head-SHA archive and persist a bounded impact snapshot
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
- The optional Proof sidecar implements the existing port over a bounded Unix
  socket. The API owns authentication, evidence grading and audit; the trusted
  executor owns the pinned image and container limits. Only the executor can
  control the dedicated Docker daemon. Requests cannot choose mounts, images or
  environment variables; replies bind the exact file/command request and image.
  See [deployment and limitations](operations.md#dedicated-proof-executor).
- Validated GitHub installation tokens use a bounded process-local LRU and
  refresh once per installation under concurrent demand; a 401 evicts only the
  matching stale token so the next delivery attempt refreshes it.
- GitHub App comment upserts match both the stable marker and configured App bot
  login, so a copied marker in an untrusted PR comment cannot redirect the update.
- New PR tasks persist the validated `base.sha` and `head.sha` from the accepted
  webhook before queue publication. Workers fetch GitHub's compare endpoint for
  those exact commits, persist the bounded Diff and its SHA-256, and never fall
  back to the mutable PR URL when a pinned comparison fails. Only tasks created
  before this contract use the stored URL compatibility path.
- Dynamic Skills run out of process and are snapshotted by content hash.
- `review-context@1` contains only source-labelled title, Spec and standards text.
  API sections are rejected above their UTF-8 limits; oversized GitHub PR bodies
  are explicitly marked truncated. When repository evidence is requested, the same
  pinned archive may fill an otherwise empty standards section from allowlisted root
  documents and changed-path ancestor `AGENTS.md` files. The initial normalized object
  participates in task idempotency; archive enrichment is atomically snapshotted before
  workflow execution and participates in input digests. Neither grants permissions,
  tools or routing.
- `repository-evidence@1` is server-derived and contains no raw archive or source.
  A GitHub archive is fetched only when the pinned workflow references `$input.evidence`;
  it is bounded by compressed/indexed bytes, member count, path rules, file count and
  output lists. Manual reviews and failed archive/index operations persist an explicit
  unavailable value. The snapshot is pinned into handoff identity and resume checks.
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
- Prometheus uses only fixed workflow (`review`, `studio`, `custom`) and Agent
  role/kind classes. Exact step, Agent revision and workflow revision remain in
  the tenant-scoped checkpoint snapshot, so operator drill-down does not create
  attacker-controlled or per-version metric series. Provider-reported input and
  output tokens are likewise split only by bounded request purpose.
- A durable Agent checkpoint carries the current attempt's start time and measured
  handler/output-validation duration. The console projects only the readable
  duration; implementation digests and raw timing metadata remain in the
  manage-authorized diagnostic response.
- The Operations console reuses tenant-scoped audit, admission, Queue DLQ and
  Outbox APIs. Its transport projection exposes only action/resource/time,
  capacity counters and delivery identifiers needed for an explicit replay;
  audit detail, queue/Outbox payloads and stored error text remain server-side.
- The Repository Governance console reuses the versioned policy API. Browser
  writes carry the version previously read; the existing policy lock rejects a
  stale version before history or audit is appended. Its projection exposes the
  effective policy, installed capability identifiers and version headings, but
  not tenant identifiers, historical policy bodies or arbitrary metadata.
- Built-in agent stages are trusted `AgentSpec` implementations; their contracts
  and wiring are replaceable at startup. Untrusted code remains sandboxed.
- Studio model Agents keep their identity, objective and operating instructions
  as a structured Playbook inside the existing immutable Agent definition. The
  same digest also covers model, versioned Agent Skill and typed-port choices.
  Agent Skills are either pre-model evidence tools or server-owned reasoning
  policies and declare their required input types; they cannot route the Workflow.
  The server, not the Playbook, appends Skill, output-schema and trust-boundary
  instructions. Legacy Prompt/tool definitions remain byte-for-byte compatible
  for pinned task recovery.
- Curated Agent recipes are server-owned draft starters, not runtime plugins.
  Selecting one copies a validated definition into the ordinary editor; no recipe
  identifier is persisted, so later catalog changes cannot mutate an Agent version,
  workflow bundle or pinned task.
- The standard `api.run()` entrypoint accepts trusted workflow/reviewer contributions.
  Canary, shadow and evaluation rebuilds must retain the startup workflow revision;
  a stateful factory cannot replace the graph under an existing qualification.

The review loop is ordinary Python using stdlib topological ordering. Agent
handoffs are validated on both sides, copied as bounded JSON and committed before
dependants execute. A pinned flow/implementation/input manifest prevents mixed
revision recovery; the existing generation-fenced, first-write-wins checkpoints
now cover each agent stage as well as the outer task phases. The stdlib runner
executes each topology-ready wave with at most four concurrent nodes, merges
results in stable topology order and starts joins only after all inputs commit.
The existing specialist pool remains separately bounded inside its own node.
See [agent composition and handoff semantics](agent-workflows.md), including
at-least-once side effects and the trusted-handler timeout boundary.

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
- Contract/revision handoff errors are permanent queue failures; temporary provider
  errors retain bounded retries. An already successful or cancelled task wins over
  a late handoff error before the queue failure is classified.
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
