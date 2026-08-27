# Operations

## Health

- `/health` reports process liveness only.
- `/ready` checks PostgreSQL, queue worker/heartbeat and schema version, and
  reports GitHub/LLM circuit states; concurrent probes share one short-lived
  dependency check. An open downstream circuit does not eject every healthy
  replica, so intake can remain available while affected work fails fast.
- `/ready` separately reports optional Docker-backed Proof and Repair capability
  state. Repair is configured only when both the immutable container image and
  repository test command are present; a disabled optional capability does not
  eject review replicas.
- Startup fails when `EVOAGENT_SKILLS_DIR` is missing; use an existing empty
  directory when dynamic Skills are intentionally disabled.
- Non-loopback startup requires `EVOAGENT_SKILL_REQUIRE_CONTAINER=true`; an
  installed dynamic Skill then also requires a preloaded container image.
- Sign a release manifest with `EVOAGENT_SKILL_SIGNING_KEY=... evoagent-sign-skill
  path/to/skill.json`; the command refreshes the source hash and atomically writes
  the canonical manifest signature used at startup.
- Dead Outbox rows remain visible in `/ready` and metrics without removing every
  healthy replica from service; a stopped or persistently failing local dispatcher
  fails readiness.
- `/metrics` exposes bounded global Prometheus metrics to platform
  administrators; authenticated deployments must refresh the scraper JWT before
  its configured session expiry. Authenticated scrapes pass the pre-auth client
  rate limiter so invalid-token floods cannot amplify into user-store reads.

The in-process queue is for loopback development only. Non-loopback startup
requires standalone Redis Streams so tasks survive worker restarts.
Application startup also requires its configured bounded PostgreSQL pool; it
fails closed instead of silently switching to per-request connections.
Every pooled and maintenance connection applies
`EVOAGENT_PG_STATEMENT_TIMEOUT_SECONDS`; raise it explicitly for a measured
long-running migration instead of allowing runtime queries to wait forever.
The HTTP listener caps accepted connections before creating handler threads;
tune `EVOAGENT_MAX_HTTP_CONNECTIONS` from file-descriptor and memory limits.

## First checks

| Symptom | Check |
| --- | --- |
| Intake 5xx/latency | `/ready`, PostgreSQL pool, rate/admission limits, oldest outbox age |
| Stale outbox | store connectivity, dispatcher liveness, publish failures |
| Stale queue | Redis connectivity, worker and heartbeat liveness, handler duration |
| Dead letters | classify the root cause, resume one failed task, then a bounded batch |
| GitHub failures | allowed host, transport/sustained-5xx circuit state, rate-limit headers and installation-token refresh |
| Model failures | route host/policy, circuit state, redaction/output validation counters |
| Repair/Proof failures | container availability, image, limits and bounded command output |

## Monitoring target down

The bundled rules expect the Prometheus scrape job to be named `evoagent`.
If every target is down or the job disappears, check service discovery and
scrape authentication first, then instance liveness and `/ready`. Restore at
least one healthy target before trusting any application-derived SLO or alert.

Preload configured Skill and repair images in the Docker daemon available to
the EvoAgent process. Request execution never pulls images; configured images
are inspected during startup, resolved to immutable local image IDs and fail
closed if the CLI, daemon or image is unavailable. The Skill image must contain the EvoAgent runtime at
`/app/evoagent`; startup resolves its local immutable image ID, and execution
mounts no application or Skill source from the Docker host.
Repair/Proof worktrees are copied through the Docker client into a bounded
container tmpfs; they also require no shared host path.

Every response carries `X-Request-ID`. Unexpected HTTP errors return a generic
message; correlate the identifier with structured logs. Persisted operational
failures use `operation [type=...; ref=...]` and never store exception text.
Graceful service shutdown flushes the process-owned OpenTelemetry provider after
background work stops, so the final batch of spans is exported before exit.
While draining, probes remain available but new application requests receive
`503` with `Retry-After`; only work admitted before SIGTERM may use the grace period.
If the drain budget expires, dependencies stay open for process teardown rather
than being closed underneath a live worker; embedded callers may retry `close()`
after work drains, while the orchestrator must enforce its termination grace
period. Redis entries that race with shutdown before active
registration remain pending for lease-based reclaim by a healthy worker.

## Availability fast burn

Check `/ready`, split 5xx by release, and stop the rollout before restarting
dependencies. Preserve task IDs and traces for the incident record.

## Availability slow burn

Freeze nonessential releases, compare failure classes and tenant distribution,
and schedule remediation before the remaining error budget reaches zero.

## Intake latency

Compare intake rate with PostgreSQL pool waiters, outbox age and CPU. Shed burst
traffic through configured admission limits before increasing concurrency.

## Queue or outbox stale

PostgreSQL retains committed intent. Restore the failing dispatcher/Redis path;
`evoagent_queue_healthy=0` covers Redis connectivity, worker and lease-heartbeat
health even when queue age cannot be collected. Check `/ready` for the safe failure
reference before restarting the affected replica.
`evoagent_outbox_dispatcher_running=0` means the local dispatcher thread stopped,
even if no backlog has formed yet. Restart the unhealthy replica after preserving
its logs; do not manually alter rows or stream entries.
Sustained `evoagent_outbox_lease_conflicts_total` growth means publication is
outliving its lease or PostgreSQL is stalling. Check process and database latency
before increasing `EVOAGENT_OUTBOX_LEASE_SECONDS`; queue message keys keep retries
idempotent while the lease is recovered.
`evoagent_effect_lease_conflicts_total` applies the same diagnosis to GitHub
comment and repair ownership. Confirm provider latency and concurrent retries;
the losing worker stops before its next write and durable replay performs recovery.

## Metric collection

`evoagent_metrics_gauge_scrape_failures_total` counts scrapes that omitted one
or more dynamic gauges because a dependency probe failed. Compare `/ready` with
the missing queue, Outbox or PostgreSQL-pool series; restore that dependency
before treating an absent series as zero.

## Dead letters

Fix the root cause, resume one failed task through `/v1/tasks/<task-id>/resume`,
verify its terminal state and external effects, then resume a bounded batch.
For a successful task whose GitHub delivery exhausted retries, the same endpoint
publishes only the missing delivery effect and never recomputes the review.
Repeated delivery resumes coalesce while that Outbox/queue attempt is active;
an Outbox `dead` row or queue DLQ transition reopens the task for an operator retry.

## Review failures

In **Task Center**, select a task to inspect **Agent handoff records** above its
JSON report. Each node shows its persisted status, attempt count, update time and
uncompleted upstream dependencies. Expand **Handoff contracts and identifiers**
to inspect input sources, versioned input/output types, Agent revision, generation,
idempotency key and payload digests; payload bodies are not displayed in this panel.
Use the top refresh button to reload the list and both detail panes. The panes load
independently: a workflow API failure does not hide the task report, and switching
tasks or logging out invalidates older detail responses. The overview's built-in
role examples are illustrative, not a live execution graph. This is a read-only
inspector, not a workflow editor or a worker-liveness monitor.

The dependency-free frontend regression check runs in CI and locally with Node.js
18 or newer: `node --test tests/web.test.cjs`. It covers escaped metadata, absent
and pruned records, request failures, out-of-order responses, refresh, logout and
fix-action isolation; browser checks are still required for layout/accessibility.

Use `GET /v1/tasks/<task-id>/workflow` to locate a failed or blocked custom Agent.
The read-authenticated, tenant-scoped snapshot includes contract/wiring metadata,
attempts, generations, digests and safe error references, not input/output bodies.
`running` records dispatch, not a live worker or exclusive lease: inspect task state
and queue health before resuming through the existing task-resume endpoint.
`availability=not_recorded` means no definition snapshot was stored yet (including
legacy tasks); `pruned` means execution artifacts were removed by retention.
Handoff contract/revision failures go directly to the existing DLQ, without retrying
the same invalid graph. Check the failure reference and deploy a corrected, verified
revision; do not manually copy checkpoints between revisions. Factories used by
canary/shadow/evaluation must return the startup graph, not reload mutable wiring.
See [Agent handoff contracts](agent-workflows.md) for retry and idempotency semantics.

Separate policy rejection, reviewer failure, model transport and Skill failure.
Do not weaken evidence gates to restore throughput.
`evoagent_review_attempts_failed_total` counts failed worker execution attempts; compare
it with terminal failures in the Review dashboard before changing retry policy.
The dashboard excludes failed attempts whose admission is still active from its
failure count and calculates success rate from completed success/failure outcomes.
Overview and Task Center label those active failed attempts as `retrying` while
keeping the persisted workflow state unchanged for recovery.
The tenant failure-rate notification clears automatically after at least the
configured minimum sample count recovers to the threshold or below. Every terminal
success or failure recalculates it; `evoagent_alert_evaluation_failures_total`
means the review outcome is durable but notification refresh needs investigation.
`evoagent_release_observation_failures_total` means the durable outcome did not
reach canary governance; pause that rollout and restore PostgreSQL before relying
on its error budget. Stable-lane outcomes do not require a rollout observation.
`evoagent_review_terminal_accounting_failures_total` means a synchronous request
raised without a matching durable `FAILED` or `CANCELLED` state; restore PostgreSQL
and inspect the task before retrying the request.
`evoagent_failure_case_persistence_failures_total` means the task was safely marked
failed but its learning-corpus record was lost; check PostgreSQL/schema health before
using failure-case counts for evolution decisions.
`evoagent_review_agent_budget_timeouts_total` means the combined specialist pool
exceeded the task deadline; reduce or speed up Skills before increasing the budget.
`EvoAgentReviewAgentBudgetTimeouts` opens a ticket after more than two timeouts in
15 minutes; correlate the panel with Skill failures before changing the task budget.
`evoagent_review_agent_output_limit_rejections_total` means the combined specialist
output exceeded the bounded review size and was stopped before downstream work.
Use `evoagent_skill_runs_total`, `evoagent_skill_failures_total`,
`evoagent_skill_timeouts_total`, `evoagent_skill_sandbox_failures_total`,
`evoagent_skill_output_rejections_total`,
`evoagent_skill_output_limit_rejections_total`,
`evoagent_skill_container_cleanup_failures_total` and the
`evoagent_skill_execution_seconds` histogram to separate capacity, sandbox and
protocol failures. The task timeline's `agent_failure.sender` identifies the
specific Skill without adding user-controlled metric labels.
`EvoAgentSkillFailureRateHigh` opens a ticket only after more than 20 executions
and a sustained 15-minute failure ratio above 5%; compare the classified
counters, then inspect recent `agent_failure` senders before rolling back the
immutable Skill set.
`EvoAgentSkillContainerCleanupFailing` pages on any failed removal. Restore the
container runtime, inspect exact `evoagent-skill-*` leftovers, preserve incident
metadata and remove those containers before resuming the affected Skill set.
`EvoAgentRepairContainerCleanupFailing` applies the same response to
`evoagent-verify-*` repair containers.
Growth in `evoagent_github_comment_truncations_total` means complete reports no
longer fit comfortably in PR comments; use the task report until a measured need
justifies publishing authenticated report artifacts.
Disabling a repository or its `post_review_comments` policy acts as a delivery
kill switch: workers re-read it after review execution and at the provider write
boundary, after any existing-comment scan. Closed, draft, and superseded review
sessions are checked at the same boundary.
Draft PR webhook deliveries intentionally have no task ID. `closed` and
`converted_to_draft` end the session, cancel unfinished turns and suppress
pending GitHub comments; `reopened` and `ready_for_review` reopen the same
session with a new turn. Human input can reopen only a same-tenant session that
is still `input-required`; it cannot override a concurrent webhook close/draft.
The state transition and secret-free operator audit commit in one transaction.
Event time is fenced in PostgreSQL, so a delayed older
delivery is recorded but cannot reverse the newer lifecycle state. Do not replay
draft deliveries to force early work.
`reviewer.revision_mismatch` means the accepted application/model/Skill set is
no longer running. Restore that revision or submit a new review after rollout;
do not edit the task snapshot or reuse its checkpoints. Tasks admitted before
execution binding was introduced are rejected and must be submitted again.

Cancelling a queued task atomically records `CANCELLED` and releases its tenant
admission slot even if Redis is unavailable. A running task keeps its slot until
the bounded reviewer call returns, but cancellation wins every later state or
success commit and is acknowledged without queue retry or a DLQ alert. Results
that return after the task budget are discarded before checkpoint persistence.
An overlapping synchronous HTTP review returns `409 Conflict` instead of
reporting an internal service failure after that cancellation wins.
The cancellation endpoint also returns `409` with the current durable state when
the task already reached a non-cancelled terminal state.

## Repair outcomes

Inspect the container image, command, timeout and resource limits. A verifier
abstention is safer than publishing an unproven edit.
Completed verification results retain the execution mode, resolved container
image and SHA-256 of the exact test command without persisting the command itself.
The same bounded attestation and verdict are included in `repair.create` audit
events, including idempotent replays; test output remains only in the effect result.
For a new repair, the effect receipt and audit event commit in one PostgreSQL
transaction, so a successful publication cannot become durable without its audit record.
When `EVOAGENT_REPAIR_TEST_COMMAND` is empty, repair publication stops before
reading or writing GitHub state; single-file compilation alone is not accepted
as regression evidence. Configure the repository test command and submit a new
review task before retrying.
Disabling the repository or `auto_fix`, or removing an applied rule from the
allowlist, blocks a verified repair before its GitHub branch/PR writes.
The source PR must also remain open, non-draft and on the reviewed head before
each missing branch/PR write. A closed, draft or changed PR requires a fresh
ready review; never edit the stored head snapshot.

## Quality feedback

Sample false positives, missed issues and bad fixes by rule. Change a rule only
with a reproducible evaluation case.
Feedback is tenant-fenced on the task row; the learning sample and a
content-free operator audit commit together.

## Model rollout

`POST /v1/deployments/llm-review` accepts only existing positive
`stable_version`/`candidate_version` values, integer canary/shadow percentages,
bounded error/disagreement rates, a positive sample count and a boolean
`auto_promote`. Auto-promotion requires non-zero shadow traffic. Invalid or
unknown fields are rejected before PostgreSQL or the audit log. The candidate
must have an `approved` evaluation qualification; rejected, deferred and legacy
versions cannot be candidates. The stable lane accepts only approved versions or
pre-v18 legacy baselines. A valid configuration, its normalized audit record and
the returned rollout generation share one transaction.
An approved candidate must also carry the current application/model/Skill execution
revision. After any runtime revision change, re-run qualification before configuring
a replacement version as a candidate; an identical Prompt is eligible for a new
version only after the runtime revision changes. Missing or stale evidence fails closed.
Existing running deployments are checked again during assignment: a new-revision
replica suppresses stale candidate and shadow traffic, keeps the stable lane, and
increments `evoagent_release_revision_mismatch_total` until the candidate is requalified.

Each admitted task snapshots the stable version, candidate version and rollout
generation. PostgreSQL counts canary and shadow observations only while that
exact generation is still running, so delayed queue work cannot roll back or
promote a replacement rollout—even when an operator retries the same candidate.
Repeated delivery of one task is also counted once, so retries cannot inflate
the canary error budget or sample size used for automatic promotion.
If a rollback changes the Prompt selected for an interrupted task, its existing
workflow snapshot cannot be resumed under that replacement Prompt. The same check
applies when all Agent work and outer harness results were cached before the
final task commit failed, including a concurrently completed cache entry. This is
a permanent handoff failure, not a reason to edit snapshots or increase retries.
Resume only while the original execution is still permitted by rollout policy;
otherwise submit a new review. Tasks that have not begun Agent execution may
still use the existing stable-lane fallback. Already-successful reports remain
historical results and are not recomputed for delivery retries.
Automatic rollback and promotion update the deployment and append their metrics
to the operator audit in one transaction; alerts are notification, not history.
`evoagent_release_alert_failures_total` means that durable rollout state and
audit succeeded but the operator notification did not; inspect the alert store
without retrying or reclassifying the observation.
`evoagent_shadow_audit_failures_total` means even the fallback audit for a
shadow failure could not be persisted; restore PostgreSQL and correlate the task
with the in-process failure metric before the evidence ages out.

## History retention

Keep retention disabled until policy approval. If enabled maintenance stalls,
check PostgreSQL locks and batch limits; never broaden deletion predicates.
Old shadow observations are deleted only after their rollout stops running, so
retention cannot weaken retry deduplication or change promotion evidence.
After the configured age, successful/cancelled tasks keep their report, final
trace anchor and audit metadata but lose raw Diff payloads, checkpoints and
agent messages; completed primary Outbox intents are pruned with the same safety
window. Completed comment/repair effect receipts also expire at that boundary;
provider markers and deterministic repair branches reconcile a later retry, while
in-progress receipts are never pruned. `input.execution_artifacts_pruned_at` makes
execution cleanup explicit.
Webhook delivery claims expire at the same boundary. Startup rejects a retention
age that does not exceed `EVOAGENT_WEBHOOK_MAX_AGE_SECONDS`; older replays then fail
the API age gate instead of creating duplicate tasks.
FAILED task artifacts remain available for operator resume.

## Tenant review capacity

Sustained rejection means a tenant owns too many non-terminal reviews. Resolve
or finish existing tasks before raising the bound.

## Queue recovery

A live Redis handler heartbeats its pending entry. Do not increase the reclaim
threshold to hide heartbeat failures. Restore Redis connectivity; only a dead
handler should become reclaimable. Never edit stream/outbox records manually.
Inspect the tenant-scoped DLQ, then resume the failed task through
`POST /v1/tasks/<task-id>/resume`; this reacquires tenant capacity, publishes a
fresh delivery generation and records the operator audit transactionally.
Both queue backends retain approximately the newest 10,000 DLQ entries as a bounded
incident buffer; resumable task state remains in PostgreSQL rather than depending on it.
Queue envelopes carry metadata only and are capped at 256 KiB before JSON parsing;
large Diff payloads remain in PostgreSQL.
Deferred GitHub tasks refetch the diff from their snapshotted URL when the first
fetch never completed.
Offline reconstruction also restores a successful task whose explicit comment
redelivery was published to Redis but never completed; ordinary successful
tasks remain terminal and are not replayed.

## Deployments

Run one EvoAgent process per container and scale containers horizontally.
PostgreSQL pools are capped at 256 connections per process; increase replicas
rather than removing this database protection.
Set the orchestrator termination grace above
`EVOAGENT_SHUTDOWN_GRACE_SECONDS + EVOAGENT_QUEUE_SHUTDOWN_TIMEOUT_SECONDS`;
the built-in Compose files use 40 seconds for the default 30-second drain budget.

1. pause review and webhook intake at the ingress;
2. wait for Outbox pending/publishing, queue depth and active review admission
   to reach zero;
3. back up PostgreSQL, verify migrations on a disposable copy, then run one
   `evoagent-migrate` job against production;
4. deploy the immutable application/Skill set and verify every `/ready`
   response reports the expected `reviewer_revision`;
5. run one review, resume intake, then watch availability, review success,
   outbox age, queue/DLQ depth and tenant admission rejections.

This drain-first cutover is required because one Redis consumer group does not
route by application revision. Add revision-routed worker pools only when
zero-downtime deployment is a measured requirement.
The bundled `scripts/deploy.sh up` therefore refuses to replace a running
EvoAgent; after steps 1–2, run `down` before starting the new revision.

Roll back application code only if it understands the active forward-only
schema. Redis carries no irreplaceable state.

## Security defaults

- Treat an invalid boolean environment value as a startup error; do not rewrite
  or coerce configuration typos during deployment.
- Keep `EVOAGENT_DEFAULT_TENANT_ID` stable; startup rejects empty, surrounding-
  whitespace or over-200-character values before any tenant-scoped data is written.
- Require JWT authentication before binding beyond loopback.
- Non-loopback startup also requires positive per-client rate, per-instance
  heavy-concurrency and cross-replica tenant-review limits. Tune all three from
  measured traffic; keep ingress limits as defense in depth.
- Trust forwarded addresses only from explicit ingress CIDRs.
- Keep GitHub, JWT and model secrets in a secret manager.
- Configure GitHub App ID and private-key path together. Outside loopback,
  any GitHub Webhook intake requires the complete tenant-bound GitHub installation
  OAuth configuration and a Webhook HMAC secret of at least 32 bytes; PAT-only,
  default-tenant fallback, weak rotation secrets and incomplete credentials fail startup.
- Rotate the bootstrap administrator password through `POST /v1/auth/password`
  after first login and whenever credentials may be exposed. The endpoint
  requires the current password, commits the credential change and secret-free
  audit row together, and invalidates every existing JWT and GitHub OAuth state;
  log in again afterward.
- Create additional local members through `POST /v1/users`. Tenant admins may
  grant tenant roles in their current tenant; only a platform admin may grant
  `platform_admin`. User, membership and secret-free audit rows commit together.
- Offboard or restore a local identity through `POST /v1/users/status`. Because
  account activity is global across memberships, only platform admins may use
  it; self-disable and disabling the last active platform admin fail closed.
  Every real status change revokes existing JWT and OAuth state versions.
- Rotate `EVOAGENT_AUTH_SECRET` by deploying the new value with the old value in
  `EVOAGENT_AUTH_PREVIOUS_SECRET`; after every replica is updated, wait for old
  sessions and GitHub OAuth states to expire and verify
  `(sum(rate(evoagent_auth_previous_secret_verifications_total[15m])) or vector(0)) == 0`,
  then remove the previous value.
- Rotate the GitHub webhook secret by deploying the new current secret with the
  old value in `EVOAGENT_GITHUB_WEBHOOK_PREVIOUS_SECRET`, updating GitHub, then
  removing the previous value after old deliveries drain and
  `(sum(rate(evoagent_github_webhook_previous_secret_verifications_total[15m])) or vector(0)) == 0`.
- Require a container for untrusted repair verification.
- Keep model and GitHub egress on exact host allowlists.
- Leave history retention disabled until backup and legal policies approve it.

For restore procedures see [disaster recovery](disaster-recovery.md).
