# Operations

## Health

- `/health` reports process liveness only.
- `/ready` checks PostgreSQL, queue worker/heartbeat and schema version, and
  reports GitHub/LLM circuit states; concurrent probes share one short-lived
  dependency check. An open downstream circuit does not eject every healthy
  replica, so intake can remain available while affected work fails fast.
- `/ready` separately reports optional Docker-backed Proof and Repair capability
  state. Proof includes `configured`, `mode` and `healthy`; socket mode performs
  a bounded exchange with the pinned executor, while direct Docker mode verifies
  the immutable image remains reachable. Repair is configured only when both the
  image and repository test command are present. An unhealthy optional capability
  does not eject otherwise healthy review replicas.
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
`evoagent-slo` returns `no-data` (exit 2 by default) while all targets are down or
the job is absent, even if historical samples exceed the minimum. Do not use
`--allow-no-data` for release qualification. A current healthy target is only a
minimum collection check; verify replica discovery and the entire observation
window separately.
The CLI reads the [current target list](https://prometheus.io/docs/prometheus/latest/querying/api/#targets)
instead of trusting a historical `up=1`. The PromQL alert remains subject to
[series staleness](https://prometheus.io/docs/prometheus/latest/querying/basics/#staleness):
after removing a job, its last sample may remain visible for the default
five-minute lookback, followed by the alert's two-minute pending period.
Do not promise a two-minute page from the moment a scrape job is deleted.

Configure the existing authenticated endpoint with Prometheus's native
[`authorization.credentials_file`](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#http_config).
For a private Compose network (use HTTPS with certificate verification outside
the isolated local network):

```yaml
global:
  scrape_interval: 15s
  scrape_timeout: 5s
  evaluation_interval: 15s
rule_files:
  - /etc/prometheus/evoagent.rules.yml
scrape_configs:
  - job_name: evoagent
    metrics_path: /metrics
    follow_redirects: false
    authorization:
      type: Bearer
      credentials_file: /run/secrets/evoagent-metrics-token
    static_configs:
      - targets: [evoagent:8080]
```

Provision a platform administrator's session token through your secret-management
system, readable only by the scraper identity. It has platform privileges, not a
metrics-only scope: do not commit it, embed it in an image, expose it in command
arguments or use it for untrusted PR code. Renew it before `expires_in`, atomically
replace the file in its parent directory, then verify the target's `up` value and
`lastError`. A file-only bind mount may retain the old inode after replacement;
mount the credential directory read-only for the scraper instead. Native file
rotation does not require restarting Prometheus. A configuration change requires
a [SIGHUP reload](https://prometheus.io/docs/prometheus/latest/configuration/configuration/).
Authentication failures consume the client rate limit; increasing the rate limit
does not repair expired credentials.

Run the native alert behavior checks alongside rule syntax validation:

```sh
promtool check rules ops/prometheus/evoagent.rules.yml
promtool test rules ops/prometheus/evoagent.test.yml
```

CI uses the pinned Prometheus 3.5.0 image. These tests check the two-minute
pending period, target recovery, a missing scrape job, partial replica failure,
flapping and the fast-burn sample floor. They test expressions and timing, not
Alertmanager routing or delivery to an on-call person.

## Container verification prerequisites

Preload configured Skill and repair images in the Docker daemon available to
the EvoAgent process. Request execution never pulls images; configured images
are inspected during startup, resolved to immutable local image IDs and fail
closed if the CLI, daemon or image is unavailable. The Skill image must contain the EvoAgent runtime at
`/app/evoagent`; startup resolves its local immutable image ID, and execution
mounts no application or Skill source from the Docker host.
Repair/Proof stream the client-side worktree through `docker exec -i` into the
existing `/work` tmpfs, without mounting a source path from the daemon host.
A separate isolated Python process produces the tar stream; the image must
provide `tar` supporting `--no-same-owner`, in addition to its test runtime.
Extraction runs as the same user as the tests and preserves file modes without
requiring source UID/GID ownership. The application neither buffers the complete
archive nor writes an intermediate archive file. Both processes must succeed within the
shared verification deadline; a valid partial archive is not accepted when its
producer fails. Producer failures, transfer failures and timeouts prevent test
execution and enter the existing container cleanup path.
Producer teardown has a separate five-second bound; container removal retains
its separate ten-second bound.

All untrusted Linux sandbox commands, including Skills, explicitly use UID/GID
`65534:65534`, regardless of the Docker client's OS, host UID/GID or image `USER`.
Repair/Proof gives `/work` the same ownership and mode `0700`; extraction and test
execution use that identity too. Images and test dependencies must be readable
without root, and tests must write inside the provided tmpfs. Incompatible images
fail verification; execution never retries as root. This does not change the
application process identity or the trusted direct-library host fallback.

This follows Docker's documented [tar-stream workaround](https://docs.docker.com/reference/cli/docker/container/cp/#corner-cases)
for mounts such as tmpfs while retaining the read-only root filesystem. The
shared-path dependency is removed in code, but actual Docker and remote/nested
deployment compatibility still require the container contracts on the target
runtime; local tar-process checks do not prove container isolation. For the direct
adapter, operators must provision the Docker client, authorized daemon connection
and preloaded image. The optional dedicated executor below includes its own CLI;
neither approach exposes a daemon socket to PR code.

### Sandbox owner loss

Skill and Repair/Proof launches share `container_runtime.py`. Both use a detached
container with the trusted image's **`/bin/sleep` as PID 1**, a lifetime of the
configured command timeout plus 15 seconds, and
[Docker automatic removal](https://docs.docker.com/reference/cli/docker/container/run/#clean-up---rm).
The extra 15 seconds cover the existing producer/cleanup budgets; they do not
extend the caller's command deadline. The image must provide `/bin/sleep`.
Untrusted code runs through `docker exec`, never as the deadline process.
Daemon-default init injection and image health checks are explicitly disabled.
The deadline runs as a separate non-root UID/GID `65533:65533` with working
directory `/`; test/Skill commands remain `65534:65534`. Dropped capabilities
and separate UIDs deny signals, ptrace and process-memory access to the deadline,
without assuming the host enables stricter
[Yama ptrace policy](https://docs.kernel.org/admin-guide/LSM/Yama.html).

When PID 1 exits, Linux terminates the remaining PID namespace. Members of that
namespace cannot send it unhandled signals, including stopping/killing the sleep
process from repository code; see
[Linux PID namespace semantics](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html).
This makes expiration independent of the API/Skill worker surviving. It does
not rely on untrusted Python resource limits or an additional reaper service.

Normal completion still immediately removes the exact generated container name.
If Docker has already auto-removed it, a successful name-filtered daemon query
must confirm absence; a failed query, transport error or surviving match is not
treated as cleanup success. The query never supplies additional deletion targets.
Removal and confirmation share the existing ten-second cleanup budget, and use
the same Docker connection environment as execution. Skill startup and execution
also share one command deadline; an uncertain launch never runs the Skill.

Each new sandbox also receives an absolute wall-clock expiry label. Before a new
Skill or Repair/Proof job is admitted, the owner lists only containers carrying
that label, accepts only exact generated names and numeric deadlines, and removes
only entries whose deadline has passed. Inventory is capped at 64; daemon errors,
malformed entries, excess inventory or cleanup failure reject the new job. This
pre-admission reconciliation handles a container whose monotonic sleep was frozen
by `docker pause`: after the runtime resumes, the next job removes the expired
container before starting. It does not run a daemon thread or prune by prefix.

The lifetime depends on a running kernel, not a surviving Docker daemon. In the
qualified daemon-loss drill, containerd reported the task stopped at its deadline;
Docker removed the remaining container metadata after recovery. Removal still
requires the daemon. Hypervisor suspension or runtime loss still prevents cleanup
while unavailable; pre-admission reconciliation starts only after Docker returns.
Containers launched by older releases do not have the expiry label and therefore
do not gain this behavior retroactively. Restore the runtime, inspect exact known
old-version leftovers, and never replace that inspection with a broad container
prune.

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

Task Center exposes task-level operations to users with `review` permission
(maintainer, admin or platform admin), including after a Studio tab is closed.
Select **从失败节点续跑** for a failed execution, **取消任务** for pending/running
work, or **重试结果回写** when the review succeeded but session/GitHub delivery has
not been confirmed complete. State hints do not authorize an operation: the API
still checks the current tenant, role and durable state. Task Center does not
expose Outbox identifiers or delivery credentials.

The browser **运维中心** provides the tenant-scoped operator view. Auditors can
read the latest audit actor, action, resource and time; admins can additionally
read admission capacity, dead Queue task IDs and dead Outbox identifiers. It does
not transport audit detail, queue/Outbox payloads or stored error text. Replaying
an Outbox entry requires a second confirmation, updates only a row still in
`dead`, uses the existing transaction/audit path and refreshes the page afterward.
A missing entry means another operator or recovery path already changed it; refresh
before taking another action. Queue DLQ entries link back to Task Center, where
the existing task resume contract remains the only execution retry path.

The task list, details and Studio distinguish a delivery-managed retry from a
terminal failure, and show cancellation-in-progress until the worker records the
terminal state. Cancellation is cooperative: an in-flight external call may still
finish, and completed external effects are not rolled back. Cancelled tasks cannot
resume. Missing source payloads, incompatible execution revisions or deterministic
contract errors require a new task after the cause is fixed.

Each operation requires confirmation and refreshes the persisted result. Switching
tasks or logging out closes stale confirmations; old responses cannot update the
new selection or trigger follow-up reads under a different session. An unconfirmed
or timed-out response is not treated as success and is not retried automatically;
refresh task state before deciding whether to repeat it. A retry acknowledgement
is not proof of eventual review or delivery success.

## Review failures

In **Task Center**, select a task to read its risk summary, issue cards, changed-line
evidence, and repair/test suggestions. The processing history below the report uses
readable Agent names and shows persisted status, retries and upstream dependencies.
Expand **查看处理内容与结果** for readable input/output artifacts. Raw task JSON,
Prompt snapshots, hashes, generations and idempotency keys are not rendered. Browser
requests use `X-EvoAgent-View: console`: the server returns allowlisted fields and
typed artifact summaries, not full internal records. The published Agent palette
omits Prompt/config; explicitly opening the definition editor still returns authored
configuration. Unknown artifact types never fall back to raw JSON. Unsupported
console endpoints are rejected before business side effects. JSON responses vary by
this header and remain `Cache-Control: no-store`.

Console errors return only an allowlisted `error_code`. The browser translates
known codes into actionable messages (for example, preserve edits and reload a
conflicting draft, or correct incompatible connections). Unknown errors fall back
to the HTTP status without displaying exception text, response fragments, or proxy
HTML. Admission errors use the same response boundary and retain `Retry-After`;
the browser does not automatically retry mutations. A 401 clears the current
session even when the response body cannot be parsed. The `X-Request-ID` response
header remains available for operator correlation, outside the user-facing report.

The server also enforces a separate data-access boundary. Full Studio drafts and
the deployment catalog require `manage`, with or without the view header. Raw task
details, workflow status/artifacts, and published Studio definitions require
`manage`; `read` users may request their allowlisted console views instead.
`admin` and `platform_admin` have `manage`; `maintainer` and `auditor` do not.
Permission checks run before resource lookup and use current tenant membership,
not the role cached by the browser. Administrators remain tenant-scoped.
If loading the Studio catalog is denied, the browser clears cached Studio
configuration and unsaved edits rather than continuing to render the old editor.
Previously viewed/downloaded information cannot be recalled by a role change.

Compatibility: non-admin clients previously reading raw diagnostic endpoints now
receive 403. Send `X-EvoAgent-View: console` for the limited representation, or use
`GET /v1/tasks/<id>/report` for Markdown. Definition lists and repository binding
discovery remain readable. The header never grants access to full configuration.
This is not secret detection in source code or other user-authored text, and does
not replace the separate permissions for audit logs or operational endpoints.
Use the top refresh button to reload the list and both detail panes. The panes load
independently: a workflow API failure does not hide the task report, and switching
tasks or logging out invalidates older detail responses. The overview's built-in
role examples are illustrative, not a live execution graph. This is a read-only
inspector, not a worker-liveness monitor. Use the separate **Workflow Studio** to
create Agents, connect typed ports, trial a draft, publish versions and bind repositories.
Publication does not change a repository binding. Read its current configuration,
then explicitly select a published version to activate or roll back. A concurrent
change returns 409 and requires rereading; existing tasks retain their snapshots.
Before activating a custom version, the API also applies the same current repository
pipeline/provider/model/region policy used by review intake. An incompatible route
returns 403 without changing the binding or appending a successful binding audit;
the check runs no Agents or model calls and creates no review task. Restoring the
default remains available for an enabled repository even when its model policy is
incompatible. This is a point-in-time preflight, not a policy lock: later intake and
execution still enforce policy, and no Diff-dependent size/budget or quality claim
is made. This is manual version switching, not Studio-specific approval or automatic
canary rollback.

Browser review submissions (manual and Studio) always use the existing asynchronous
intake with `Idempotency-Key`; the synchronous API remains available to API clients.
A 30-second acknowledgement timeout does **not** cancel a task. After a lost response,
retry unchanged in the same login session to recover the original task while its
record is retained. Each form keeps one pending key and a SHA-256 fingerprint in
tab-scoped `sessionStorage`, never the Diff, repository, token or workflow body.
Reloading the tab preserves that receipt, not the form: re-enter the exact input.
A confirmed run, changed input/version, or login change starts a new intent; check
Task Center before submitting again after closing the tab or switching accounts.
Login/logout clears pending receipts and private form content; stale responses cannot
populate the new review result. A storage-cleanup failure may recover the same task
once more, not duplicate it. HTTPS or loopback and usable session storage are required;
blocked/corrupt receipt storage stops submission before any review HTTP write.
There is no automatic client retry. This deduplicates intake, not arbitrary external
effects: worker delivery retains its existing at-least-once semantics. Accepted and
replayed submissions are both audited while sharing one task and one Outbox message.

Console login changes invalidate all pending page reads and action responses, not
only the selected task. Logout or a current-session 401 clears lists, reports,
prompts, form credentials, trial receipts and Studio dialogs; the background shell
becomes inert and focus moves to login. A late old response cannot refill a view,
unlock a new action, begin follow-up reads, overwrite a new login or redirect to an
old GitHub installation. Failed logins remain editable; 403 permission denials do
not sign the user out. Confirmed server-side work may still finish after logout.

The existing login token is shared through same-origin `localStorage`. A token
change in another tab invalidates this tab instead of silently adopting the new
account. The browser checks storage events, focus/page restoration, and request
boundaries; already-open tabs require reauthentication, and a fresh page uses the
stored login as before. This is browser data isolation, **not** server-side logout
revocation, remote token theft protection or task cancellation. JWT expiry and
existing password/user/membership checks still govern authenticated API access;
keep production authentication and HTTPS enabled.

The dependency-free frontend regression check runs in CI and locally with Node.js
18 or newer: `node --test tests/web.test.cjs`. It covers escaped user content,
non-rendering of internal metadata, report/empty/failure states, graph mutations,
absent/pruned records, request failures, out-of-order responses, refresh, logout and
fix-action isolation, plus lost-acknowledgement/reload retries and console-wide
login, expiry and cross-tab isolation. Browser checks are still required for
layout/accessibility and native storage/dialog behavior.

Use `GET /v1/tasks/<task-id>/workflow` to locate a failed or blocked custom Agent.
The manage-authorized, tenant-scoped diagnostic snapshot includes contract/wiring metadata,
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
`evoagent_repository_evidence_failures_total` means a workflow requested repository
evidence but its pinned GitHub archive could not be fetched or indexed. The review
continues with Diff and the already accepted Context Pack and stores an explicit
unavailable snapshot; repository standards are therefore not auto-filled for that task.
Successful archive processing uses the same read to populate an empty standards section
from the bounded allowlist; malformed individual standards files are skipped and mark the
Context Pack truncated without discarding otherwise valid Python impact evidence.
check installation access, the pinned revision and archive-size setting before submitting
a new review. Retrying the same task deliberately reuses its unavailable snapshot.
Do not raise `EVOAGENT_REPOSITORY_EVIDENCE_MAX_BYTES` above the measured repository
need merely to hide repeated failures; the setting bounds both download and indexed
Python source bytes and is capped at 1 GiB.

## Workflow agent failures

`evoagent_workflow_agent_runs_total`, `successes_total` and `failures_total` are the
global execution totals. Per-class families such as
`evoagent_workflow_review_agent_planner_duration` and
`evoagent_workflow_studio_agent_llm_failures_total` identify the slow or failing
part of the graph. Workflow classes are bounded to `review`, `studio` and `custom`;
Agent classes are the installed default roles, Studio `rules`/`llm`/`merge`, or
`custom`. User names, tenant IDs, repositories, task IDs and revision hashes never
become Prometheus series.

`EvoAgentWorkflowAgentFailureRateHigh` opens a ticket when more than 5% of at least
20 executions fail over 15 minutes. Start with the Agent latency/failure panels,
then use `GET /v1/tasks/<task-id>/workflow` to inspect the exact step, immutable
Agent revision, current-attempt `duration_ms`, attempt count and safe error reference.
The duration covers the Agent callback and output-contract validation, not queue
wait, upstream wait or checkpoint persistence. Checkpoint replays have their own
counter and are not counted as new Agent executions.

Actual provider-reported token counters split default review, Studio, evaluation
and other purposes (`evoagent_model_<purpose>_input_tokens_total` and
`output_tokens_total`). Use them to locate the workflow class consuming budget;
use the task's pinned workflow snapshot for the exact revision. This two-level
drill-down avoids an unbounded series for every published workflow version.

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
Repair/Proof attempts exact-name removal even when the startup command times out,
exits nonzero or raises an exception: a missing acknowledgement does not prove
that the daemon created nothing. Cleanup has a separate 10-second bound; failure
raises the existing cleanup alert metric and prevents a successful verification.
An unavailable daemon still requires operator reconciliation of leftover
containers; the application cannot guarantee their removal while it is unreachable.
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

## Dedicated Proof executor

The console's **修复验证** page submits text files for the original/patched
versions and reproduction/regression commands to the existing `POST /v1/proofs`
endpoint. It requires the `fix` permission. Output is three readable evidence
stages; test output and the patch are expandable. Infrastructure diagnostics,
internal hashes and raw result JSON are not rendered. Editing the input or
signing out clears the displayed evidence. L4 means only that the supplied
reproduction and regression commands passed on the supplied patch, not that
the whole repository is safe. Closing the page does **not** cancel an admitted
command; execution remains bounded by the executor's deadline. No automatic retry.

For a containerized Web/API, use the explicit `docker-compose.proof.yml` overlay
on a **dedicated Docker daemon/VM**. It adds a trusted `proof-executor` service:

```text
Browser → authenticated API / ProofRunner
        → private Unix socket / ProofExecutorPort
        → trusted executor → short-lived restricted test containers
```

The API mounts only the Proof socket volume, read-only, and has neither a Docker
client nor daemon socket. The executor has no network, no application credentials,
a read-only root, a bounded tmpfs, dropped capabilities and bounded concurrency.
Its Docker-socket access is nevertheless **daemon-admin authority**: `:ro` does
not make Docker API operations read-only. Never use a shared production daemon.
The execution containers do not receive either socket, host files or host secrets;
untrusted commands retain UID 65534, no-network, read-only-root and tmpfs policy,
with the separate UID 65533 deadline described above.

Preload a trusted image with the required test dependencies, build the application's
default target and the `proof-executor` target, then set `EVOAGENT_PROOF_IMAGE`
and `EVOAGENT_DOCKER_SOCKET_GID` to the socket's numeric group **on the daemon host**.
Start Compose with both files. The overlay sets `EVOAGENT_PROOF_EXECUTOR_SOCKET`
for the API and waits for the executor's health check. See the
[local deployment commands](local-docker.md) for the isolated macOS environment.
Default Compose without this overlay still has no executable Proof capability.

The trusted executor uses Docker's native `restart: always`; disposable job
containers remain `--rm` with **no restart policy**. On the qualified Docker
29.7.2 runtime without live restore, `unless-stopped` recovered after VM power-off
but did not recover after a daemon crash: daemon restoration stopped the
surviving executor. The engine's
[restore path](https://github.com/moby/moby/blob/6a43e3d/daemon/daemon.go) and
[signal handler](https://github.com/moby/moby/blob/6a43e3d/daemon/kill.go) explain
this as the manual-stop flag being set during restoration. `always` passed
both actual fault drills, not only configuration inspection.

With [Docker restart semantics](https://docs.docker.com/engine/containers/start-containers-automatically/),
manually stopping the executor keeps it stopped **until the next daemon restart**.
For persistent disablement, stop Proof admission and remove only the stopped
executor service container with Compose, preserving its socket volume; omit the
overlay on later starts. The policy does not enable VM boot-at-login, change the
base API/database/queue restart policies, retry an interrupted Proof command,
or replay an unfinished review. Use the full Compose startup procedure for the
whole application, and the disaster-recovery runbook for durable review state.

After a daemon/VM incident, confirm daemon reachability, executor health and
absence of exact known sandbox IDs before running a fresh bounded proof.
An interrupted first reproduction remains L1/error; restarting a worker does
not promote it. The owned stale socket can be recovered, and an existing API
client can reconnect when the immutable image is unchanged. See the
[fault-acceptance boundary](integration-testing.md#daemon-and-vm-fault-acceptance);
the short test's recovery time is not a production RTO.

The private socket directory is mode `0700`; the socket is `0600`, owned by the
application UID. Protocol v1 accepts only text files and a command, not image,
mounts, environment variables or resource settings. Frames have a 16 MiB request
and 256 KiB response ceiling and a five-second transport deadline; commands are
limited to 8 KiB. Paths must be canonical relative paths with no dot-segments or
backslashes, so serialization cannot change which duplicate alias wins. The
page limits editing to 32 files; the existing API ceiling remains 5,000 distinct
paths and its HTTP/body limits still apply. The executor permits two concurrent
jobs by default and rejects excess connections instead of growing a work queue.

The API pins the executor's immutable image ID at startup. Replies must match
the request digest, command digest and that image. Malformed replies, overload,
lost connections, timeouts or an unavailable Docker daemon are inconclusive,
never a reproduced bug or host fallback. `proof.run` audit events retain these
bounded attestations, not source files/commands. The digest binds a reply within
the trusted socket boundary; it is not hardware attestation or proof against a
compromised executor/daemon. `/ready` performs a bounded live health exchange and
the console disables Proof when that check fails; it does not execute repository
code as a probe. A changed executor image requires an API restart too.

Set limits on the executor's CLI (`--timeout`, `--memory-mb`, `--pids-limit`,
`--cpus`, `--workers`); the bundled overlay uses a 120-second command deadline.
Keep the API's `EVOAGENT_REPAIR_VERIFY_TIMEOUT_SECONDS` aligned with that deadline.
An entire L4 request may take three command deadlines plus bounded cleanup.
The executor drains on SIGTERM; the overlay allows 150 seconds before SIGKILL.
An owned stale socket is recovered on restart; regular files and live listeners
are never replaced. SIGKILL of the executor is covered by the independent
[sandbox lifetime](#sandbox-owner-loss); after VM/daemon recovery, the next job
reconciles expired labelled sandboxes before launch. Executor-side verifier metrics
are currently local to that process, not automatically exported through the API's `/metrics`;
API Proof latency/evidence/inconclusive counters remain available there.

This adapter only serves manually submitted Proof jobs. It does not enable
automatic GitHub repair publication, remote Skills, or a new Workflow Studio
node. Those integrations retain their separate existing security gates.

## Repair outcomes

Inspect the container image, command, timeout and resource limits. A verifier
abstention is safer than publishing an unproven edit.
The default repair registry includes three AST-only rules with narrow semantic
preconditions: `SEC-EVAL` accepts only a one-argument builtin `eval`,
`REL-EMPTY-EXCEPT` accepts only a single `int(value)` conversion returning
`None`, and `SEC-ASSERT-AUTH` accepts only `assert user.is_admin`. Qualified
forms become `ast.literal_eval`, an explicit `TypeError`/`ValueError` handler,
and an explicit `PermissionError` branch respectively. Syntax errors, aliases,
extra arguments and other lookalikes receive no line-based fallback. The
existing YAML, Cookie, shell, secret and debug-output rules retain their own
independent preconditions.
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
Deferred GitHub tasks refetch the diff from their snapshotted
`base_sha...head_sha` comparison when the first fetch never completed. A pinned
comparison failure is retried or failed without substituting the current PR.
Only legacy tasks without `base_sha` retain the stored PR URL compatibility path.
Offline reconstruction also restores a successful task whose explicit comment
redelivery was published to Redis but never completed; ordinary successful
tasks remain terminal and are not replayed.

A Docker/VM restart can recover the services while still losing the newest Redis
AOF entries. PostgreSQL may then contain `published` Outbox rows for tasks whose
stream messages no longer exist; ordinary dispatcher restart intentionally does
not republish that ambiguous history. If non-terminal tasks remain while queue
depth, lag and pending entries are all zero, keep intake stopped and use the
offline recovery boundary:

1. Stop every API, Outbox dispatcher and worker connected to the restored
   PostgreSQL database. Do not run recovery against a live deployment.
2. Provision a new empty Redis logical database or cluster. It must not contain
   application keys; do not flush the old target as a shortcut.
3. Set `EVOAGENT_DATABASE_URL` to the restored PostgreSQL database and
   `EVOAGENT_REDIS_URL` to the empty target. Generate a new canonical UUID, then
   review a dry-run plan:

   ```bash
   python -m evoagent.recovery \
     --recovery-id <new-uuid> \
     --confirm-database <database-name> \
     --max-tasks 10000
   ```

4. Confirm the candidate/recoverable/unrecoverable counts and retain the
   returned `plan_sha256`. Applying a changed plan is rejected:

   ```bash
   python -m evoagent.recovery \
     --recovery-id <same-uuid> \
     --confirm-database <database-name> \
     --max-tasks 10000 \
     --apply \
     --expect-plan-sha256 <reviewed-plan-sha256>
   ```

5. Point the application at that reserved Redis target; start the Outbox
   dispatcher and workers before external effects and ingress. Reconcile task
   terminal states, Outbox status, active admissions, Redis lag/pending and the
   `recovery.queue.stage` audit row. Preserve the old Redis target for incident
   evidence until reconciliation finishes.

The command never copies raw Diffs into its report. Any unrecoverable candidate
fails closed unless the operator explicitly accepts it for incident handling;
that flag is not a substitute for restoring missing task intent.

## Deployments

Run one EvoAgent process per container and scale containers horizontally.
The bundled Compose file gives PostgreSQL, Redis and EvoAgent
`restart: always`; the one-shot migration job does not restart. This restores
containers after a Docker daemon or VM restart, but does not replace the queue
reconciliation above, backups, an orchestrator or production RPO/RTO testing.
PostgreSQL pools are capped at 256 connections per process; increase replicas
rather than removing this database protection.
Budget the **sum** of replica pool maxima, plus migration/backup/monitoring
connections, below the database connection limit. Replicas must share PostgreSQL
and Redis and use matching application/Skill/model revisions, authentication
configuration and tenant admission limits. On one host, assign distinct listening
ports; published host ports in the supplied Compose file cannot be duplicated by
blindly increasing its replica count. Production ingress and process supervision
remain deployment responsibilities. The [controlled Studio baseline](performance.md#studio-completion-and-replica-baseline--2026-08-28)
compares threads with replicas and distinguishes task completion from HTTP 202;
use representative workload measurements before choosing a replica count.
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
