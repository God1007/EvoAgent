# Integration testing

Local unit tests run without external services. Database tests require an
explicit temporary PostgreSQL URL and otherwise skip; Redis and container tests
follow the same opt-in pattern.
The PostgreSQL recovery test additionally requires `pg_dump` and `pg_restore` on
`PATH`, plus a test role that can connect to `postgres` and create/drop its own
disposable databases. It creates its own source and restore databases rather than
restoring over `EVOAGENT_TEST_POSTGRES_URL`.

```bash
export EVOAGENT_TEST_POSTGRES_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent_test'
export EVOAGENT_TEST_REDIS_URL='redis://127.0.0.1:6379/0'
python -m pytest -q
```

CI starts temporary PostgreSQL and Redis services and covers:

- migrations, concurrent startup and connection pooling;
- transactional outbox publication and tenant admission;
- Redis ACK, dedupe, heartbeat, reclaim, retry and durable DLQ storage;
- local container Proof and repair verification;
- the serving image under its non-root, read-only, capability-dropped runtime;
- PostgreSQL restore drills and PostgreSQL-to-Redis reconstruction;
- wheel installation and console entry points;
- an audited CycloneDX inventory generated from the hash-locked runtime dependencies.

Register every real `ReviewService.close` with `addCleanup` immediately after
construction, including services using the in-memory queue: their Outbox threads
still poll the shared database. Cleanup must run even when a test assertion fails.
Tests must use disposable databases. `tests/db_support.py` truncates application
tables between tests, so database cases must not run in parallel against the
same database; never point it at production. Redis tests likewise delete the
project's stream, DLQ and deduplication keys: use a dedicated disposable Redis
instance/database and do not run those cases in parallel against it.
CI also fails if its selected
PostgreSQL, Redis, or container contract suites skip any test. The real-service
job retains JUnit results and PostgreSQL-adapter coverage XML for boundary-level
evidence and future coverage-floor calibration.

## Local handoff verification — 2026-08-28

Verified on macOS arm64 and Python 3.12.13 with private, loopback-only PostgreSQL
16.15 and Redis 7.4.11 instances. Both were built from verified source archives:

- [PostgreSQL 16.15](https://www.postgresql.org/ftp/source/v16.15/):
  SHA-256 `c1575341fa7bd40f5274ea465b34390f4dc64cdd0770af327005caaeb9f6b7ed`.
- [Redis 7.4.11 official checksums](https://github.com/redis/redis-hashes):
  SHA-256 `3c266ece0abd54ed3b1c912c6eb86b7508cf382cb690ee6649d3843f018f6357`.

Application source SHA-256 for this historical run:
`5c6b41daaf8a289d7fb238d77fbaaf41ff169fa31e078079b763fc36c107fb5f`.
Later follow-up results below and the [controlled evaluation](evaluation-baseline.md)
have their own verification scope; they do not reassign this run to newer source.

- Workflow suite: **29 passed, 0 skipped**, plus 54 passing subtests. Real SQL
  covers committed handoffs, failed-node replay, retention, tenant isolation and
  the outer review harness. An HTTP test submits the eight-stage custom workflow
  and checks completed status without exposing source or payload bodies.
- Delivery suite: **3 passed, 0 skipped**. The parent terminates a real worker
  process after a custom Agent's running checkpoint commits. A new service
  instance reclaims its Redis delivery, preserves all seven completed upstream
  checkpoints and collaboration logs, advances the receiver from attempt 1 to 2
  with the same handoff identity/idempotency key, then ACKs and releases admission.
  The same crash point is also tested after deleting only the fixture's Redis
  queue keys. The real recovery CLI rejects a nonempty target and wrong plan hash,
  leaves dry-run state unchanged, stages one new Outbox intent, and reapplies the
  same epoch without duplicates. Restarting the original workflow preserves the
  same handoffs and completes delivery; the original published intent remains in
  history. This is logical queue-state loss, not Redis process/power failure.
  A third case rejects malformed Agent output once, enters the DLQ and leaves
  no report. No mock queue, recovery target or checkpoint store is used.
  The runbook's dry-run/review/apply shell commands also passed against one
  disposable task, using the actual console entry point and a separate empty
  Redis logical database; only the example database name was substituted.
- CI infrastructure contract selection: **76 passed, 0 skipped**, plus 56 passing
  subtests. PostgreSQL adapter line coverage: **80.89%**, measured separately with
  `--cov=evoagent.postgres_store --cov-config=/dev/null`.
- Full suite with both test URLs and PostgreSQL client tools: **869 passed,
  4 skipped**, plus 701 passing subtests. Coverage: **89.29%** using the existing
  project coverage configuration, which excludes the PostgreSQL adapter and
  sandbox runner; this is not an adapter-specific coverage percentage.
- The database retained its `Asia/Shanghai` default. The initial full run exposed
  two UTC-string contract failures; shared connection options now pin store
  sessions to UTC. Both pooled and direct connections are covered, without
  changing the database default or weakening the timestamp assertions.
- Remaining skips: **4 container tests**, because a container runtime was not
  configured. Redis worker restart/reclaim, heartbeat, socket reconnection and
  DLQ contracts ran; Redis server power loss, container isolation, remote model
  providers and real GitHub writes were not exercised. This is local evidence,
  not proof of arbitrary exactly-once side effects, production readiness or a
  claim that the new changes have passed GitHub Actions.

To repeat against your own **disposable** PostgreSQL and Redis, set both URLs above and run:

```bash
python -m pytest -q tests/test_workflow.py --junitxml=output/workflow-tests.xml
python -m pytest -q tests/test_workflow_delivery.py --junitxml=output/workflow-delivery.xml
python -m pytest -q --cov=evoagent --cov-report=term \
  --junitxml=output/integration-tests.xml
```

The temporary local servers were shut down after verification; no system service
or production database was installed or modified.

### Console follow-up verification

The handoff inspector was checked using the production HTML/CSS/JavaScript with
read-only, explicitly labeled local fixture responses. Desktop and 390px-wide
browser checks covered completed, failed, cancelled, absent, pruned and unavailable
records, expanded contracts, long identifiers, loading and rapid task switching.
The narrow layout had no horizontal overflow. These UI fixtures do not establish
backend durability; the real-service suites above cover that boundary.
`node --test tests/web.test.cjs` passes and is now included in CI. Wheel and sdist
builds pass; the wheel's four web assets match the working-tree bytes.

The first follow-up full run had **859 passed, 1 failed, 4 skipped**: the worker
crash/reclaim test did not reach its expected running handoff within 15 seconds.
Its task was failed with no node checkpoints. An isolated rerun, a subsequent full
run and ten delivery-only repetitions passed, but did not explain the first failure.

### Recovery failure diagnosis and fixture cleanup

A further full run reproduced the failure. The task's audit log recorded
`reviewer.revision_mismatch`: the rejecting runtime revision matched the default
`ServiceTests`/`ServiceSessionTests` configuration, not the custom recovery worker.
Two service tests and the session-test setup never closed their real services.
Their surviving Outbox threads could claim the later test's committed task and
deliver it through an old in-memory queue. Revision pinning correctly rejected
that delivery before any workflow checkpoint was written.

The fixtures now register `addCleanup(service.close)` immediately. The older
asynchronous review test also uses cleanup so an assertion failure cannot leave
its dispatcher behind. A deterministic regression check runs the three previously
leaking fixture paths and checks that their services and Outbox threads are closed:
all three subtests failed before the fix and pass afterward. Production queue,
handoff validation, retry budgets and timeouts were not changed. The full suite
now passes with the totals above; the recovery assertion retains task state/error
diagnostics for any future failure.

Ten mixed-order repetitions then ran the three formerly leaking fixture paths
followed by both real PostgreSQL/Redis delivery tests: **50 passed**, with **zero
remaining Outbox dispatcher threads** afterward. Unlike the earlier delivery-only
repetition, this includes the predecessor fixtures that triggered the interference.
To run the relevant regression group, use
`python -m pytest -q tests/test_service.py tests/test_workflow_delivery.py` with the
disposable test URLs set.

### JSON workflow entrypoint verification

The checked-in `examples/business-review.json` assembles the same eight-stage
workflow as the Python example. CLI preflight and offline execution report the
same revision, and offline execution still detects `SEC-EVAL`. The real HTTP test
now loads this JSON definition, submits a review through `ReviewService`, and
checks that all eight handoffs complete.

The regression check also covers startup-only file snapshots, rebinding current
catalog handlers, preflight without Agent execution, and delegation to the normal
API entrypoint. Invalid JSON, duplicate fields, non-object roots, non-standard
numbers, oversized files and unknown Agent names are rejected. The JSON cannot
request arbitrary Python imports; it is trusted deployment configuration, not a
PR-controlled plugin interface. These checks do not qualify a custom graph for
production or prove that its business rules preserve the default evidence gates.

### Outer-cache resume binding

A real PostgreSQL regression reproduced a version-check bypass: after all Agent
and outer harness checkpoints completed, an interrupted final task commit left
the task failed. Resuming with another Prompt reused the outer cached result and
committed success without consulting the workflow manifest. The new assertion
failed before the fix.

Every outer `executing`/`reviewing` cache acceptance now checks the existing
workflow snapshot, including a winner committed after the initial inventory read.
The test uses the real `GatewayReviewer` with a fixture model gateway and real SQL.
Its initial inventory is made stale to exercise the late-winner read path.
Changed Prompt and Diff inputs are rejected without changing checkpoints or
making another model call. Restoring the original Prompt/input succeeds with
exactly one total model call. The harness clears its temporary thread-local Diff
reference on both success and failure. No extra checkpoint table, version store
or retry policy was introduced. This is a controlled resume fault test, not a live-provider
or multi-replica rollout qualification.

The controlled 100-case evaluation was rerun for the source digest above. Its
metrics and both failing production-release gates remain unchanged; see the
[evaluation baseline](evaluation-baseline.md).

### Installed workflow artifact verification

Both distributions now include `custom_review_workflow.py` and
`business-review.json`; wheel installation places them under
`share/evoagent/examples`. Previously neither artifact contained these resources.
The CI smoke check verifies the installed import location, application source
fingerprint, and exact example bytes in the checkout, sdist and installed wheel,
then runs preflight and the eight-stage offline example outside the checkout.

The exact CI check and documented commands passed locally in a fresh virtual
environment without system site-packages. The check rejected an older same-version
wheel; ordinary pip installation skipped its replacement, while the explicit
`--force-reinstall` now used by CI installed and verified the new artifact.
The related offline suites passed **34 tests and 54 subtests**. This artifact check
uses no database, container or remote model and does not prove deployed service
readiness; it also does not mean the uncommitted changes have run on GitHub Actions.

### Backup/restore handoff verification

The real recovery test interrupts the eighth custom Agent, invokes the DR CLI
parser with real `pg_dump`/`pg_restore`, then restores the retained dump into another
fresh database and resumes the same workflow. All seven upstream Agent checkpoints
remain unchanged, the specialist is not called again, and the receiver advances
from attempt 1 to 2 with the same idempotency key. The restored task succeeds while
the source remains failed and unchanged. Generated databases are cleaned up on
both assertion failure and success.

The source defaults to `Asia/Shanghai`; the final restore target explicitly uses
UTC. Before the fix, the drill failed integrity validation despite identical schema
and row counts: six tables hashed equivalent `timestamptz` values differently.
The shared fingerprint serializer now normalizes aware datetimes to UTC while
preserving naive timestamps and dates. The recovery suite passes **7 tests** and
is included in CI's zero-skip infrastructure selection. Reproduce it with
`python -m pytest -q tests/test_dr.py` using the disposable database and tools above.
This proves a fresh logical backup's local restore/resume path, not historical
backup retention, point-in-time recovery, regional failover or production RPO/RTO.

### Repair/Proof startup acknowledgement loss

The shared verifier now takes cleanup responsibility before invoking `docker run`,
not only after its successful return. Fault injection first reproduced leaked
container names for startup timeouts, nonzero exits and exceptions; the regression
now verifies exact-name removal, no repository execution and no host fallback.
Cleanup failures still fail closed through the existing metric and error path.

Local verification with the disposable PostgreSQL database passed **177 tests and
103 subtests** across `test_verifier`, `test_proof`, `test_fixer`, `test_skills` and
`test_application_use_cases`; Ruff and mypy also passed. **Five container tests
remain skipped** because this host has no Docker CLI/image. The added CI contract
starts a real container, injects loss of its startup acknowledgement, and checks
that this exact container is gone; it has not yet run here or on CI for this diff.
This is not evidence of real container isolation or daemon-outage cleanup.

### Client-side worktree transfer

The verifier's daemon-side `/source` bind mount has now been removed. An isolated
stdlib Python producer streams an archive to `docker exec -i ... tar`; extraction
runs as the test user in the existing bounded tmpfs. The source path is never a
Docker mount argument, and the application creates no intermediate archive file.

Real local Python/tar subprocess checks cover a multi-megabyte private file,
nested and hidden paths, spaces/commas/Unicode/colons, preserved modes and symlinks,
and unchanged source files after destination edits. A separate case verifies that
successful extraction cannot mask a failed producer. Receiver failure, timeout
and missing executable cases assert that the producer is reaped and its pipe
closed. The related suite now passes **181 tests and 111 subtests** with the
disposable PostgreSQL database; Ruff and mypy pass.

Argument-level fixtures simulate root/non-root POSIX and non-POSIX clients.
They reproduce and prevent host UID `0:0` inheritance and omission of the Skill
user flag: both sandbox launches and all Repair/Proof execs explicitly use
`65534:65534`, with matching private worktree ownership. This is not a real
Windows-client or container-runtime qualification.

The five Docker contracts remain unexecuted locally. Both execution probes require
UID/GID `65534:65534`; the Repair probe also checks readable/writable copied `0600` files, an actual
`/work` tmpfs, no `/source` mount, and an unchanged client file, in addition to
the existing network/root-filesystem/environment checks. Their syntax was checked
locally, not its isolation behavior. Remote-daemon and nested-container deployment
remain unqualified until these contracts run in those environments; they are not
proved by the absence of a bind-mount argument.

### Console availability acceptance — 2026-08-28

“The page loads” is not acceptance of every action. The console now starts write
buttons disabled until the server supplies the current account's capabilities.
`/api/dashboard` exposes only role/permission flags and GitHub configuration
presence, never credentials. This is not a live provider health check; the POST
handlers still enforce current permissions, policy, immutable versions and limits.

| UI path | Automated evidence | Remaining deployment acceptance |
| --- | --- | --- |
| Agent authoring, publication, typed connections, workflow versions and repository binding | `tests/test_studio.py` with disposable PostgreSQL; `tests/web.test.cjs` covers draft preservation, incompatible connections, pagination and exact-version selection | Recheck the current visual interface in a permitted browser |
| Manual Diff review and draft/published trial | Real HTTP/SQL Studio contracts; browser-script tests cover submission receipts, failed acknowledgements and session changes | A live model-backed workflow needs its configured provider and repository policy |
| Reports and handoff inspection | Explicit response allowlists, escaped readable output, per-task stale-response and partial-failure tests | Unknown artifact types intentionally show an explanatory message, not raw JSON |
| Cancel/resume and repair confirmation | Browser-script confirmation/target tests; backend lifecycle contracts | Remote review delivery and repair publication still require real GitHub qualification |
| Model Agent publication | Missing/removed models disable publication before any draft write; saving a draft remains possible | Configured routing does not prove provider credentials, budget or availability |
| GitHub installation | Missing authentication, App/OAuth or Webhook setup disables installation; permission/configuration matrix covered | OAuth consent, installation binding, reachable Webhook and real PR processing not tested locally |
| Evolution evaluation | Disabled until model/dataset status is ready; failed or stale reads cannot unlock actions | Live evaluation, independent holdout provenance and release gates remain required; a deferred result is not reported as a completed evaluation |
| Repair branch creation | Server checks role, PR snapshot, policy/rules, installation binding, credentials, isolated runtime configuration and test command; unknown conditions fail closed | Actual container tests and GitHub writes remain unqualified; a button permits an attempt, not guaranteed repair coverage |

The latest availability slice passed the HTTP/Studio/projection group with
**27 tests and 83 subtests**, and all **6 browser-script test groups**. These are
real PostgreSQL/HTTP tests plus a minimal DOM harness, not a new full-browser run.
The full Python suite passed **897 tests and 820 subtests**, with **15 skips**:
nine Redis contracts, five container contracts and one missing PostgreSQL-client
restore contract. Supplying the existing private PostgreSQL client binaries then
passed all **7 disaster-recovery tests**, including that restore contract. Redis
and container contracts were not rerun with their required services in this slice.
Repository-wide Ruff, mypy (63 source files), JavaScript syntax and diff checks pass.
The in-app browser rejected navigation under its URL policy; it was not bypassed
with another browser or page-fetch mechanism. Current visual/browser interaction
acceptance remains outstanding. No model credentials were supplied, no Docker
runtime was installed and no GitHub installation or write was performed.

Repeat the automated slice against a **disposable** PostgreSQL database:

```bash
python -m pytest -q tests/test_http_server.py tests/test_studio.py tests/test_console_view.py
node --test tests/web.test.cjs
```

### Studio cross-process handoff qualification — 2026-08-28

An independent loopback Redis instance and the disposable PostgreSQL database
were used to run the same seven-file infrastructure selection as CI:
**95 tests and 126 subtests passed, with zero skips**, also checked in the JUnit
report. This includes Redis heartbeat, reconnect, pending-message reclaim,
deduplication, DLQ, recovery CLI and database restore contracts that were skipped
in the preceding console-only slice. No original deployment service or preview
database was changed.

The added Studio case publishes a three-Agent graph in a non-default tenant,
terminates its worker process after both rule outputs commit and the merge is
marked running, then publishes and binds a different Agent/workflow v2. A new
service in another process reclaims the pending Redis message. It invokes only
the unfinished merge, retains all completed checkpoints and the original bundle,
and preserves the receiver's handoff/idempotency identity while advancing its
attempt from 1 to 2. A fresh task uses v2 and produces its different expected
report. Both tasks ACK, release admission and leave no DLQ item; another tenant
cannot read the first task.

This exercises the real authoring component, task intake, SQL checkpoints,
Outbox and Redis worker. Test-only wrappers pause the merge and observe actual
Agent calls; they do not supply simulated rule results, checkpoints or queues.
The existing scheduler passed without production changes. The case is included
in the existing CI file selection, but this local result is not a GitHub Actions
result. It does not qualify Redis server power loss, live providers, GitHub
effects, Docker isolation or current browser interactions.

The subsequent full run passed **908 tests and 820 subtests**, with only the
**5 container contracts skipped** because no Docker runtime/image was configured.
All six browser-script test groups, repository-wide Ruff and mypy passed. Fresh
wheel and sdist builds succeeded; the wheel contains all **63 Python modules and
6 web assets**, byte-identical to the working tree, including Studio and console
projections. This was package-content verification, not a new installed-service
or browser smoke test. The private test Redis was stopped after verification;
the user's running preview and its existing Agent/workflow data were preserved.

### Source-distribution and installed-package qualification — 2026-08-28

Extracting the previous sdist outside the checkout reproduced collection errors:
the archive included test modules but omitted `tests/db_support.py`, the tests
package marker, scripts and lock files. `MANIFEST.in` now explicitly includes the
test/deployment inputs, frontend tests and synthetic evaluation fixture. Only
`.env.example` is included; real environment-file variants remain excluded.
CI now asserts imports originate in the extracted tree, runs its Python and
browser-script suites, and compares installed web assets and examples with both
the checkout and sdist. This catches missing Studio resources as well as missing
Python modules without relying on the editable checkout installation.

The initial fixed archive passed **908 tests and 820 subtests**, with only five
Docker contracts skipped, plus all six frontend-script groups. Rebuilding its
wheel from the extracted tree produced identical contents for all **84 wheel
entries**; this compares entry bytes, not ZIP timestamps or archive hashes.
A new virtual environment installed that wheel offline while reusing the existing
local dependency packages. Its default web path resolves inside the installed
prefix, and the packaged eight-stage example executes with the expected finding.
This verifies package isolation and resources, not a fresh dependency download,
live browser, or live-provider deployment.

The installed CLI check additionally exposed that `evoagent-migrate` ignored
arguments: even `--help` attempted configuration/database initialization. Argument
parsing now precedes configuration and rejects unsupported options (especially
`--dry-run`) without opening a store. A regression check verifies that help and
invalid arguments never read settings or connect, while the existing no-argument
migration and JSON-error contracts remain intact. Installed CLI help checks are
also part of CI.

After the CLI fix, the rebuilt sdist passed **909 tests and 823 subtests** from
its own extracted directory with real disposable PostgreSQL/Redis; the only
**5 skips** were Docker contracts. Its six frontend-script groups passed. The
final wheel loads all seven entry points from the installation prefix, all six
management CLI help commands pass, and `evoagent-migrate --dry-run` is rejected.
Rebuilding that source archive again preserves all 84 wheel-entry contents.
Ruff, mypy, YAML parsing and the CI shell syntax checks pass locally. The temporary
Redis was stopped afterward; no original deployment migration, GitHub push or
external-service action was performed. Current changes still await a real CI run
and the previously listed deployment qualification.

### API command-line startup boundary — 2026-08-28

The API command had the same argument-handling defect as the migration command:
`python -m evoagent --help`, `--dry-run` and unsupported port/positional arguments
reached deployment configuration instead of exiting. A regression reproduced
this before the fix without opening a store or starting workers. Both the module
and installed console script now route through one standard-library parser before
`api.run()`. The programmatic `run(workflow_factory=..., reviewer_contributions=...)`
entrypoint remains unchanged so custom deployment scripts retain their own CLI.

The five-file HTTP/workflow/Studio/migration/dependency selection passed **73 tests
and 143 subtests, with zero skips**, both in the checkout and in a freshly extracted
sdist using disposable PostgreSQL. All six frontend-script groups, Ruff, mypy and
dependency checks passed. A fresh wheel installation outside the checkout passed
the exact CI Python smoke block: both API CLI forms exit correctly even with
deliberately invalid deployment configuration, packaged resources match, and the
eight-stage custom workflow executes. CI bounds these CLI subprocesses by a timeout.
The project virtualenv's console script was regenerated with an offline editable
install; the running preview process and original deployment were not restarted.
This targeted run is not a new full-suite, live-browser or production qualification.

### Cancellation while a Studio Agent is running — 2026-08-28

`tests/test_workflow_delivery.py` now covers the HTTP cancellation boundary with
real PostgreSQL, Redis and a separate worker process. It publishes a rule Agent
and a downstream merge, lets the rule compute an actual finding, then pauses its
return before checkpoint commit. The parent submits `POST /v1/tasks/{id}/cancel`
through a real loopback HTTP server, verifies the persisted cancellation flag,
and only then releases the computed result.

The worker exits cleanly with the task `CANCELLED`, no report, unchanged
checkpoints and no merge invocation. Admission is released and the Redis message
is ACKed without a dead letter. Delivering the original intent again through
Redis does not enter the review executor or change the task/checkpoints. The
test uses the normal local-development principal in an explicit fixture tenant;
it is not a fresh authentication or browser acceptance test. Fault injection
delays the real rule result; it does not fabricate storage or queue outcomes.
The production runner needed no change. This qualifies cooperative cancellation
of a returning Agent, not forcibly terminating arbitrary code or undoing remote
effects. The existing infrastructure CI selection already includes this test.

The seven-file infrastructure selection passed **96 tests and 126 subtests with
zero skips**, confirmed in JUnit. The full checkout suite then passed **912 tests
and 827 subtests**; its only **5 skips** were unconfigured Docker isolation
contracts. All six frontend-script groups, Ruff and mypy passed. The independent
test Redis was empty and stopped after verification. The running preview and its
data were preserved; no production code change, restart, commit or push was needed
for this cancellation qualification. These are local results, not a remote CI run.

### Release-evidence audit — 2026-08-28

Replaying the CI evaluation-evidence assertions found that the baseline document
still named an older application source fingerprint. The unchanged 100-case
dataset was rerun against source SHA-256
`826d8bc92a4f028d36266f050694b1708d3a612e9c517861b8b326a707a6d683`;
the [snapshot](evaluation-baseline.md) now records that run. The same CI assertions
pass with only the report output path substituted. Candidate F1 remains 82.5%
and E2E security-fix rate remains 35.0%, below its unchanged 60.0% minimum; neither
the repair-coverage nor production-data provenance gate was relaxed.

The current checkout's full suite with disposable PostgreSQL, Redis and PostgreSQL
client tools passed **912 tests and 827 subtests**, with only the **5 unconfigured
Docker tests skipped**. Coverage is **90.17%**, above the configured 70% floor;
the existing exclusions for `__main__`, the PostgreSQL adapter and Skill runner
still apply, so this is not adapter or sandbox coverage. All six frontend-script
groups, Ruff, mypy and `pip check` passed. This is local evidence for the working
tree, not proof that its unpublished changes passed remote CI.

### Live documentation walkthrough — 2026-08-28

Normal in-app browser access became available for the documentation pass. Against
the running local API and PostgreSQL preview, the browser opened the saved
three-Agent workflow, submitted a new `local/readme-demo` task against published
v1, refreshed its completed state, and opened the report and merge handoff.
The UI showed two findings (one high, one medium), three completed nodes, and two
upstream inputs containing one finding each. The manual-Diff repair action stayed
disabled with its missing-GitHub-snapshot explanation. Draft and repository
binding state were not changed. Five unmodified screenshots and the exact Diff
are in the [demo walkthrough](demo.md).

This supersedes the earlier browser-access blocker for this narrow normal-path
walkthrough, not the unexecuted authenticated, destructive, provider, GitHub,
mobile or container acceptance cases. It does not establish that every console
control has been exercised against a live production dependency.

### First PR container run and portable transfer fixture — 2026-08-28

[PR #12's first infrastructure job](https://github.com/God1007/EvoAgent/actions/runs/33146603533/job/98768926321)
passed against commit `98ed9d92aefb25c13ac74625e0b5c4d3e360fb88`: **96 contracts
and 126 subtests**, followed by **5 real Docker verifier tests**, all with zero
skips. Production-image migration, readiness/Outbox delivery, graceful shutdown
and queue-reconstruction steps also passed. This is Linux CI evidence, not a local
Mac container or production-deployment qualification.

The same run exposed a portability bug in the host-side transfer
test: a missing source produced empty input that the local tar accepted but the
CI tar rejected. The intended test requires successful extraction followed by a
producer failure. Its fixture now uses the real archive producer and receiver,
adds an explicit exit code 7 after a valid archive is complete, and asserts the
receiver returned 0, the file was actually extracted, and the overall transfer
returned 7. Production transfer handling and rejection criteria are unchanged.

### Local isolated Docker acceptance — 2026-08-28

After explicit installation authorization, a separate Lima VM (`evoagent-docker`)
was started on macOS arm64: 4 CPUs, 4 GiB RAM, a 24 GiB sparse disk, and no host
directory mounts. Docker Engine/CLI 29.7.2 use a private local socket and client
configuration; the global Docker context and other VM were not changed. The
Compose project `evoagent-isolation` has its own PostgreSQL 16.15/Redis services,
volumes and loopback ports. Tests use a separate disposable database and Redis
logical database, not the running application's data. See the
[local startup, login and shutdown instructions](local-docker.md).

Final application source SHA-256:
`b38722d4345f9f6fd6783be355d457053d6ba56096ac301b7171d313d4208607`.
The locally built image `evoagent:local-b38722d4345f` resolves to
`sha256:6a4e8f0f6de782bed6cd4b050e863e27a15a0a0e46230a57047e95043ae0569b`.
These identify a local working tree/image, not a published release or CI result.

The first five real container checks produced **3 passes and 2 failures**. Skill
startup stripped `DOCKER_HOST`/client configuration and attempted the default
socket instead of the dedicated daemon. Docker-only settings now reach only the
trusted CLI, and cleanup uses the same environment snapshot. The host Python
fallback stays sanitized. Both sandbox launch paths also explicitly clear the
uppercase/lowercase proxy variables that Docker can automatically inject from
[client configuration](https://docs.docker.com/reference/cli/docker/#automatic-proxy-configuration-for-containers).
A synthetic proxy-credential configuration first demonstrated automatic injection
in a positive control; the real sandbox tests then verified those values and
host secrets do not reach either Skill code or repository tests.

A repeated full run found an independent cross-clock bug: the Outbox age metric
reported **−0.019469 seconds**, because its creation timestamp comes from the
application and its age query uses the database clock. A deterministic real-SQL
regression moves the creation timestamp ten minutes ahead/behind the database;
the future case failed before the fix. The shared adapter now floors elapsed age
at zero, matching the queue's existing behavior, while preserving positive ages
and counts. No delivery, lease, retry or timestamp-writing behavior was changed.
This does not replace clock synchronization for multi-host deployments.

Final results with the production image and disposable Docker-backed services:

- **920 tests and 831 subtests passed, zero skipped**. The full run used the
  synthetic Docker proxy configuration too. Coverage is **90.18%** under the
  existing configuration; the PostgreSQL adapter and Skill runner remain excluded
  from that percentage. Ruff, format checks, mypy, `pip check` and all six frontend
  script groups passed.
- **Six real container cases** cover snapshot Skill execution, non-root identity,
  source-transfer ownership and unchanged host files, tmpfs/read-only/network
  boundaries, bounded noisy output, timeout cleanup, and lost startup
  acknowledgement cleanup. The added Proof case executes all three steps in real
  containers, observes `failed → passed → passed`, and reaches L4 with the same
  immutable image attestation on every step. No Skill/verifier containers remain.
- The authenticated production Compose API served HTML/CSS/JavaScript, accepted
  three user-defined Agents and a published merge workflow, dispatched a review
  through PostgreSQL Outbox and Redis, and completed all three handoffs with
  `SEC-EVAL` and `BIZ-LOG` findings. An idempotent retry returned the same task;
  console responses omitted internal metadata and exposed both merge inputs.
  This is real HTTP acceptance, not a new browser interaction/screenshot pass.
- The API stopped with exit code 0. Recreating it preserved the administrator,
  published workflow and completed task; another review on the final image also
  succeeded. The initial bootstrap password was removed from container settings.
  The API remains non-root, read-only and capability-dropped, without a host or
  Docker-socket mount.

Local evidence is retained outside Git under
`$HOME/Library/Application Support/EvoAgent-Docker/evidence-20260828.zXnQTH`:
the original failing container/full-suite reports, final
`full-suite-clock-fixed.xml`, `coverage-clock-fixed.xml`, build logs, proxy
positive control and `compose-final-smoke.json`. The previous local Docker
blocker is resolved; historical results above retain their original scope.

The standard Compose API still reports Proof/repair execution as unconfigured:
these real Proof tests use the trusted host-side adapter to reach the VM. They do
not grant Docker control to the Web container. No external model call, real
GitHub write, VM power-loss drill or production SLO qualification was performed.
The controlled evaluation was rerun at the final source digest, with unchanged
35% repair coverage and unchanged failing production-release gates.

### Private Proof executor and console acceptance

The next 2026-08-28 slice closes the deployed-API gap above. The API now uses the
existing `ProofExecutorPort` through a private Unix socket; only an explicitly
enabled executor sidecar receives the dedicated VM's Docker socket. The Web/API
image has no Docker client or daemon socket. Both services run as UID 999 with
read-only roots; the executor has no network or application credentials. The
shared Proof socket is mode 0600 and its directory 0700; the API volume is mounted
read-only. Test containers retain UID 65534, no network and no host/source mounts.
Docker-socket authority is still daemon-admin authority, not a sandbox for the
trusted executor itself. See [deployment constraints](operations.md#dedicated-proof-executor).

Verification on application source SHA-256
`4c3d8b8b865fffe80dd10a56928919f6d38a67153525efac6622f69df7b62f97`:

- **929 tests and 857 subtests passed, zero skipped**, using the isolated
  PostgreSQL test database, Redis DB 1 and real Docker. Coverage **90.08%**, with
  the same exclusions as above. Eight frontend script groups, Ruff, formatting,
  mypy and `pip check` passed.
- Seven real container contracts now include a socket-to-Docker L4 proof with
  distinct request-bound attestations. Socket regressions reject unsupported
  fields, path aliases/escapes, malformed/oversized/partial frames and tampered
  receipts; they cover capacity, health probes during execution, lost connections,
  stale socket recovery and preservation of live/non-socket paths.
- The first full run found two build-contract failures: a mutable default
  Compose image tag and a pinning check that treated Docker stage aliases as
  external images. The tag was removed, and the check now permits only previously
  declared stage aliases while retaining digest pins for external images.
  A subsequent test expectation was corrected to the existing POST-auth 403
  contract; no permission check was relaxed.
- Actual authenticated HTTP requests reached L4 with `failed → passed → passed`.
  Every reply matched the exact file/command digest, command digest and pinned
  image. PostgreSQL `proof.run` records retained those attestations without source
  files or command text; the console response omitted hashes and diagnostic fields.
- Actual browser operations covered sample confirmation, file addition/removal,
  one-version-only files, input edits clearing old evidence, sign-out clearing
  private inputs, re-login and L4 execution. The UI shows three human-readable
  stages, expandable output and a patch, not a JSON response.
- Stopping only the executor left the API alive but made Proof inconclusive
  (L1/error); the page displayed “无法判断”. Restarting the same-image executor
  restored L4 without a host fallback. A previously published three-Agent workflow
  and administrator survived deployment; a new review completed all three handoffs.

The final screenshot pass removed the Proof panel's inherited 360px inner-scroll
limit; production Python and JavaScript were unchanged. The final `-ui1` images
were rebuilt, redeployed and retested with the seven real container contracts
and authenticated HTTP/browser execution. Original, unmodified viewport captures
are in [the demo](demo.md#6-在独立容器里验证修复).

Local evidence is kept outside Git in
`$HOME/Library/Application Support/EvoAgent-Docker/proof-acceptance-20260828.pyqdsG`:
the earlier failing reports, `full-suite-qualified.xml`,
`coverage-qualified.xml`, `container-ui-final.xml`, deployment/outage/recovery
JSON, final build logs and a hash manifest. This is a working-tree acceptance,
not a GitHub CI result or published release. The rerun controlled evaluation
still reports 35% end-to-end repair coverage and fails the same release gates.

### Sandbox owner-loss acceptance

The next 2026-08-28 slice removes dependence on a surviving controller's
`finally` block. Fault injection first reproduced immortal Repair/Proof and
Skill containers: killing each owner left its container present after the
23-second observation window. Both exact, test-owned containers were then
removed; no application data or unrelated containers were deleted.

The fix reuses Docker automatic removal and the trusted image's `/bin/sleep`
as PID 1, with a lifetime of command timeout + 15 seconds. The deadline uses
UID/GID 65533; extraction, tests and Skills retain 65534. This separation denies
same-user ptrace and process-memory interference without relying on host Yama
defaults. Both identities are non-root, with all capabilities dropped. The
normal command budget is unchanged; see [lifecycle constraints](operations.md#sandbox-owner-loss).

Final application source SHA-256:
`8500af9f4a9abd933a3bf555d426f3711604d03d6972287c5d97d812d8d3d87b`.

- **934 tests and 875 subtests passed, zero skipped** in **120.68 seconds**,
  using real Docker, the isolated PostgreSQL test database and Redis DB 1.
  Coverage **90.09%**, with the unchanged exclusions above. Ruff, formatting,
  mypy, dependency consistency and all eight frontend script groups passed.
- Eight real container contracts include two owner-SIGKILL subtests, plus
  signal, ptrace and `/proc/1/mem` denial against the independent deadline.
  Timeout, uncertain-start cleanup, private worktree, output limits, proxy/secret
  exclusion and both local/socket L4 execution remain covered.
- The **actual deployed Proof sidecar** was also killed after a uniquely marked
  request reached its sandbox. With a temporary five-second command deadline,
  the request became L1/error, the API stayed responsive, and the 20-second
  lifetime container auto-removed about **20.123 seconds after SIGKILL**.
  The normal **120-second** executor configuration was restored, became healthy,
  and a new request reached L4. No sandbox leftovers remained.
- Authenticated HTTP rechecks retained request/command/image-bound receipts,
  matching PostgreSQL audit entries, metadata-free console output and the
  previously published workflow. No GitHub or model call was made.

Final local images are `evoagent:local-8500af9f4a9a`
(`sha256:572d922da9ed68d1423e87412f2337b146f7578ce917a2522403d05b8e784252`)
and `evoagent-proof:local-8500af9f4a9a`
(`sha256:384f5ac22bcaeb62efbea19cab7a951462bca6f1eb53b7f155219d60f42e22e7`).
Evidence remains outside Git at
`$HOME/Library/Application Support/EvoAgent-Docker/crash-acceptance-20260828.lTXeTn`:
`orphan-before.xml`, `full-suite-final.xml`, `coverage-final.xml`,
`service-fault-final.json`, `proof-final.json`, build logs and a hash manifest.
This qualifies owner-process loss on the running local VM/daemon, **not** VM
suspension, daemon loss, VM power loss or production availability. It remains
working-tree evidence, not a published GitHub CI result.

### Remaining release qualification

The architectural and handoff evidence above does not establish a production
release. On 2026-08-28, the [main CI run](https://github.com/God1007/EvoAgent/actions/runs/33100828640)
passed for `46927e5424379169dbe2386874d505a46fe6084c`; that snapshot does not
qualify later commits. Check each proposed release's exact commit in Actions.
Local container isolation and the deployed API's private Proof executor are
covered by the acceptance above. Live provider/GitHub behavior, automatic repair
publication, hypervisor/VM suspend behavior and deployment SLOs are not yet qualified.
The later [paused-sandbox acceptance](#paused-sandbox-reconciliation-acceptance--2026-08-29)
qualifies pre-admission cleanup after a Docker container pause, not a VM suspend.
The later [full-application recovery drill](#full-application-vm-recovery) qualifies
one bounded local PostgreSQL/Redis/API power-loss and offline queue-reconstruction
path; it is not a production RPO/RTO result. The later
[deterministic repair qualification](#deterministic-repair-coverage-qualification)
closes the controlled repair-coverage gate. Independent-data provenance still
fails by design on the synthetic corpus.
The [performance audit](performance.md) records the corrected generator timing
and accounting, real HTTP failure/backlog checks, and a controlled three-Agent
completion/replica comparison with reconciled task and checkpoint counts. Representative
end-to-end Agent load and deployment SLO qualification still remain; local
toolchain checks do not replace them. These are outstanding requirements, not
waived gates.

The later [authenticated bounded-container baseline](performance.md#authenticated-bounded-container-baseline--2026-08-28)
adds six real Docker/PostgreSQL/Redis load phases: 3,472 accepted custom workflows
completed, including two distinct backpressure gates and recovery. It qualifies
that short rule-Agent workload, not representative production PR/provider load
or the 30-day SLO.

### Authenticated monitoring acceptance

On 2026-08-28, a separate `evoagent-monitoring-zay8th` Compose project exercised
Prometheus 3.5.0 against the authenticated API, PostgreSQL and Redis in the same
isolated Lima VM. No host directory mounts, production credentials, external
models or GitHub writes were used. The API was limited to 1 CPU/512 MiB and
Prometheus to 0.5 CPU/384 MiB; published ports were loopback-only. Scrape and
evaluation intervals were 2 seconds, while the shipped alert's two-minute
pending period was unchanged. A **test-only 60-second session TTL** made genuine
credential expiry reproducible; the normal experience deployment retained its
default session TTL.

The drill found and fixed two real defects:

- The SLO evaluator could report `pass` from historical samples while all
  scrapes failed with HTTP 401. It now checks Prometheus's current target list
  before evaluating achievement, preserving observed sample counts but
  returning `no-data`/exit 2 without a healthy `evoagent` target. A guard using
  only `up` was insufficient: removing the job left its previous `up=1` visible
  during Prometheus's lookback period.
- `ModelGateway.breaker_state_code` was a property although the metrics registry
  requires a callable. With no model configured it registered the integer zero,
  so `/metrics` returned 200 while silently omitting the LLM breaker gauge and
  incrementing the gauge-failure counter. The gateway now exposes a callable
  returning the current numeric breaker state; closed/half-open/open transitions
  and the no-provider case have a runnable regression check.

| Real check | Observed result |
| --- | --- |
| Unauthenticated `/metrics` | HTTP 401; authenticated scrape succeeds |
| Empty initial time series | All three objectives `no-data`, exit 2 |
| Before/after-fix load | Two batches of 300 async reviews at 10/s; 600 durable `SUCCESS` tasks, no HTTP errors |
| New-image intake HTTP p99 | 11.20 ms for this short rule-only fixture |
| New-image metrics | LLM breaker gauge present; dynamic gauge-failure counter remains zero |
| Expired scraper token | HTTP 401, target down; original alert fires after its two-minute pending period |
| Credential-file replacement | Scraping and SLO evaluation recover without restarting Prometheus |
| Removed scrape job | Current targets empty; fixed CLI returns `no-data` even with over 300 historical samples |
| Restored job | Target up and SLO evaluation pass again |

The first missing-job harness deadline of 160 seconds was too short, **not** a
passing alarm test. Actual removal at approximately 09:23:42 UTC entered alert
pending at 09:28:41 and reached firing after the additional two minutes. The
retained raw target/alert snapshots and seven native `promtool test rules` cases
cover this lookback-plus-pending behavior, missing jobs, recovery, flapping,
partial replica failure and the fast-burn sample floor. CI now runs these
behavior tests in addition to checking rule syntax. See the
[monitoring runbook](operations.md#monitoring-target-down) for credential rotation
and the distinction between CLI checks and alert timing.

Final application source SHA-256:
`a5f83d819fba47e58b787c8870e8df9a0a90d89d805b4440c94199a927ef5e03`.
Local full regression: **938 passed**, zero skipped, **90.09%** coverage. Eight
real container contracts plus two owner-loss subtests were rerun against
`evoagent:local-a5f83d819fba`; all passed. The normal 8082 deployment was updated
to that application image and `evoagent-proof:local-a5f83d819fba`; its L4 Proof,
receipt/audit binding and existing saved workflow passed again. The controlled
evaluation was rerun under the new source identity with unchanged metrics and
the same two release-gate failures.

Private evidence is under
`~/Library/Application Support/EvoAgent-Docker/monitoring-acceptance-20260828.ZaY8th/`:
Compose overrides, the finite drill, target/alert/SLO JSON snapshots, load results,
test logs and package artifacts. Prometheus `increase()` extrapolates sampled
counters, so its displayed sample counts can exceed the exact 300-request batch;
the accepted/completed counts above come from HTTP results and PostgreSQL.
These are local integration checks, **not** a 30-day production SLO, full replica
discovery/coverage proof, Alertmanager notification-delivery test, live provider
qualification or published GitHub CI result.

After the fixes, credential rotation sustained healthy scrapes for over five
minutes: the gauge-failure counter stayed zero and both monitoring alerts cleared.
The temporary stack and its three data volumes were then removed. Its 600-task
database backup was restored into a separate temporary database and reconciled
before teardown; the backup and seven exported monitoring time series remain in
the private evidence directory. The normal 8082 deployment and its data remain
available. No permanent Prometheus service or notification channel was installed.

### Daemon and VM fault acceptance

On 2026-08-28, a **separate disposable VM** (`evoagent-fault-zl5ynk`) tested
the Proof executor against Docker daemon loss and forced VM power-off. The
normal 8082 environment was not restarted or faulted. The new Lima 2.1.4 VZ
guest had 2 CPUs, 3 GiB RAM, a 12 GiB sparse disk, no host directory mounts,
and Docker 29.7.2 installed from the official installer. It used the same
preloaded `a5f83d819fba` application/executor images; application Python was
unchanged. No application database, user credentials, provider or GitHub access
was copied into this VM.

A host-side client connected through a private Unix-socket relay, so it survived
guest failure. The trusted worker used the guest user's UID 501 for this relay;
untrusted command/deadline identities remained 65534/65533. Worker limits were
1 CPU/512 MiB/128 PIDs, with a five-second command timeout and 20-second sandbox
lifetime. Each injection occurred only after observing the actual reproduction
process inside its uniquely named sandbox.

| Fault/check | Observed result |
| --- | --- |
| Baseline | Original fails, patch passes, regression passes: L4 |
| SIGKILL dockerd, hold it down past sandbox lifetime | Original request L1/error; containerd reports sandbox STOPPED within the 23-second observation window |
| Daemon restoration after held outage | Remaining sandbox metadata removed; new proof L4 with `restart: always` |
| SIGKILL dockerd, leave native systemd recovery enabled | Executor recovers without a Compose start; interrupted proof L1, fresh proof L4 at about 21.4 seconds from drill start |
| Force-stop and restart only the disposable VM | Same executor container automatically returns healthy; existing client reaches L4 about 11.4 seconds after power-off |
| Post-recovery listing | Successful Docker queries show no remaining verification sandboxes |
| Main experience | 8082 remains healthy throughout the isolated drill |

The held-outage check queried **containerd task state while Docker was down**;
a failed Docker listing was never counted as absence. Sampling was every two
seconds: the final run observed STOPPED about 21.0 seconds after SIGKILL, not a
precise 21-second deadline. Docker performed metadata cleanup after restoration.
The first harness also masked `docker.socket`, which lost systemd socket
activation on recovery; its failing journal is retained. Later held-outage
tests masked only `docker.service` and restored the socket/service explicitly.
The separate automatic-restart test did not change systemd policy or restart
services on behalf of Docker.

The drill exposed missing executor restart policy. An initial
`unless-stopped` fix passed the VM test but **failed daemon-only recovery**:
Docker's no-live-restore startup stopped the surviving executor and marked it
manually stopped. The final overlay uses native `restart: always`, tested in
both fault paths. Only the trusted service restarts; untrusted job containers
remain non-restarting and automatically removed. Manual-stop implications are
documented in the [runbook](operations.md#dedicated-proof-executor).

Evidence is retained outside Git under
`$HOME/Library/Application Support/EvoAgent-Docker/daemon-acceptance-20260828.Zl5yNK`:
VM and Compose configuration, finite fault drivers, initial failures,
`daemon-always.json`, `daemon-auto-restart.ndjson`, `vm-power-loss.json`,
process/container inspection and regression reports. These are local working-tree
checks, not a GitHub CI result. Final regression: **939 tests and 886 subtests
passed, zero skipped**, in **126.32 seconds**, with **90.09%** coverage and the
unchanged coverage exclusions. This run used the same real `a5f83d819fba`
sandbox image, isolated PostgreSQL test database and Redis DB 1. Eight frontend
script groups, Ruff, formatting, mypy and dependency checks also passed.
The 8082 worker received the same restart policy through `docker update`,
without restarting the API or executor or rebuilding unchanged Python images.
Authenticated raw/console Proof rechecks reached L4; the saved custom workflow
remained available.

The temporary worker and its VM were then removed, reclaiming approximately
3.5 GiB of VM storage. No application data lived there. The VM recipe, test-image
archive and reports remain in the evidence directory for reconstruction; the
main VM, 8082 application and its database were preserved.

They qualify **this Proof-only configuration**,
not VM pause/suspend semantics, all Docker versions, real provider/repository
loads, physical storage loss, database/Redis recovery or production RPO/RTO/SLO.

### Paused sandbox reconciliation acceptance — 2026-08-29

Application source SHA-256:
`46365b0c1ba17dab97e8785c7cfd51d97a448eb4c35a6e6b062c82b12c7c7544`.
The application/sandbox image was rebuilt as `evoagent:local-46365b0c1ba1`.
A real sandbox was created through the production command builder with an
already-expired absolute label, then placed in Docker's `paused` state. A new
container verifier job used the same isolated daemon: its pre-admission pass
removed the exact paused sandbox before launch, and the new test command passed.
The test cleanup retained the exact generated name and never scanned or removed
unlabelled containers.

The complete real-container selection then passed **9 tests and 2 subtests** in
**46.40 seconds**. Existing checks still cover owner SIGKILL, timeout, uncertain
launch acknowledgement, Skill snapshot execution, non-root/network/read-only
isolation and direct/socket L4 Proof. Unit checks separately bind the label to
the configured timeout plus the existing 15-second grace and reject malformed or
more than 64 labelled inventory entries. This qualifies container-pause recovery
before subsequent work on this isolated Docker version; it does not qualify a
hypervisor/VM suspend, cleanup while the daemon is unavailable, old unlabelled
containers, or a production multi-host runtime.

The subsequent full regression passed **951 tests and 893 subtests, zero
skipped**, in **124.48 seconds**, with **90.14%** coverage. Ruff, formatting,
mypy across 65 source modules and all eight frontend script groups passed. The
new application and Proof executor images were deployed together to 8082; all
four long-running services are healthy with `restart: always`, queue workers are
4/4, Outbox is empty, and the saved 3 Agents, 1 Workflow and 4 Studio versions
remain. A fresh authenticated Proof returned L4 with `failed/passed/passed` and
left no labelled sandbox behind.

### Deterministic repair coverage qualification

On 2026-08-29, three production `SafeFixer` rules added narrowly constrained
AST repairs for one-argument `eval`, a single `int(value)` conversion guarded by
`except Exception`, and `assert user.is_admin`. They do not rewrite SQL, paths,
randomness, hashing or other findings that still require repository semantics.
Ambiguous syntax and non-matching AST shapes abstain; GitHub publication still
requires the configured isolated repository test command and every existing
policy, PR-snapshot and installation fence.

Application source SHA-256:
`669786917b94d89b61769e63c7b629be8e3ae8e74604be6104c25981cff1c5a6`.
The controlled generator was bumped to `evoagent-e2e-v2`; dataset semantic
SHA-256 is
`92e93bbc64c70e81ab8ef26b200ea4cbe092bf159ad78c061d6da7e704577263`.

- The unchanged 100-case corpus shape still yields candidate F1 **82.5%**,
  high-risk recall **94.7%**, clean accuracy **91.7%** and execution success
  **100.0%**.
- The production repairer attempted **24** eligible findings; all 24 passed risk
  reproduction, AST patch generation, compilation, risk removal and regression
  checks. Nine detected findings abstained. Safe-fix rate remains **100.0%** and
  end-to-end security-fix rate increases from **35.0% to 60.0%**.
- The quantitative release gate now passes at its unchanged 60.0% minimum. The
  production-data provenance gate remains failed, so this synthetic result does
  not permit production activation or support claims about real public PRs.
- Full local regression passed **943 tests and 886 subtests, zero skipped**, in
  **123.09 seconds**, with **90.11%** coverage. The run included real PostgreSQL
  backup/restore and Agent-checkpoint resume, Redis, and all real container
  contracts. Ruff, formatting, mypy, dependency consistency and all eight
  frontend script groups also passed.

The JUnit and coverage artifacts are retained outside Git under
`$HOME/Library/Application Support/EvoAgent-Docker/` as
`repair-coverage-20260829-zero-skip.xml` and
`repair-coverage-20260829-zero-skip-coverage.xml`. This is working-tree evidence,
not a published GitHub CI result, live provider/GitHub qualification or an
independently labelled production dataset.

### Bounded DAG branch acceptance

On 2026-08-29, the workflow runner changed from serial topology traversal to
bounded topology waves. At most four dependency-ready nodes execute in the
existing process; joins still wait for every input. Stable topology ordering is
used when results are merged. A branch failure leaves a successfully committed
sibling reusable through the unchanged handoff identity, generation fence and
first-write-wins checkpoint. A shared deadline prevents the join from starting;
trusted Python handlers still need to call `handoff.check_active()` around
expensive or external work because a thread cannot be forcibly terminated.

A deterministic barrier regression proves that two branches enter together,
then fails one branch and verifies that resume only reruns that node before the
join. A separate blocking regression covers the parallel deadline path. The
same two 150 ms wait handlers completed in 0.3137 seconds on the prior serial
runner and 0.1603 seconds after the change. This is an in-process overlap smoke
measurement, not a throughput or production latency claim.

Application source SHA-256:
`d648071c60aa94fb07d5367515f8848688ef84dbe7990c9673a66d1309f776eb`.
The final local regression passed **945 tests and 887 subtests, zero skipped**,
in **124.68 seconds**, with **90.14%** coverage; `workflow.py` reached **97.76%**.
Ruff, formatting, mypy over 65 source files and all eight frontend script groups
also passed. The JUnit and coverage artifacts are retained outside Git as
`dag-parallel-20260829-zero-skip.xml` and
`dag-parallel-20260829-zero-skip-coverage.xml`.

The 8082 Web/API was upgraded to `evoagent:local-d648071c60aa`; readiness
reported schema 28, Redis workers 4/4, an empty Outbox and the configured private
Proof socket. An in-container repeat completed in 0.1609 seconds. The existing
three Agents, one Workflow and four published Studio versions remained present;
PostgreSQL and Redis were not recreated or restarted. The separately qualified
Proof executor remains on `evoagent-proof:local-a5f83d819fba` and healthy.
This acceptance does not qualify distributed scheduling, uncooperative handler
termination, live model/provider behavior, GitHub effects or production load.

### Full-application VM recovery

On 2026-08-29, a new disposable Lima VM
`evoagent-appfault-20260829` exercised the current API image together with
PostgreSQL 16 and Redis 7. The VM had 2 CPUs, 3 GiB RAM, a 12 GiB sparse-disk
limit and no host mounts. The application, database and queue each had bounded
cgroups and Docker `restart: always`; the migration job remained one-shot. It
used loopback ports, synthetic credentials and rule-only reviews. No main-8082
data, GitHub installation, model provider or Proof socket was copied in.

Ten baseline reviews first reached `SUCCESS`. A 4,000-line fixture was then
submitted under 32 HTTP clients with one queue worker. The VM driver was sent
SIGKILL while the batch was active: 998 requests had returned HTTP 202 and two
connections failed. Starting only the VM—not running Compose—made PostgreSQL,
Redis and the API healthy in 20 seconds from the start command. PostgreSQL logged
an interrupted system, WAL redo and readiness. Its durable task count was exactly
1,008: 323 were already successful and 685 were non-terminal when first observed.

Automatic recovery advanced to 945 successes, then stopped with **63 PENDING**
tasks, zero Redis stream lag/pending entries and every original Outbox row marked
`published`. This is an important failure result: the newest Redis AOF entries
were lost, so restart policy alone cannot infer which published messages need
replay. The application was stopped and the shipped offline recovery boundary
was run against empty Redis DB 1. Its reviewed plan
`dee8adf277d1e3550422e242b127d78afcbab20d9421e7f5004dab7fb020c083`
identified 63 candidates, all recoverable and none unrecoverable. Applying that
exact digest staged 63 replacement intents while preserving 63 historical
Outbox rows and one `recovery.queue.stage` audit. After switching the application
to the reserved target, all 1,008 accepted tasks reached `SUCCESS`; a fresh
post-recovery review also succeeded.

Final reconciliation found **1,009/1,009 successful tasks**, 1,009 reports,
11,099 checkpoints, 1,072 published Outbox rows, zero active admissions, zero
terminal errors and zero Redis lag/pending entries. A 22 MiB custom-format
PostgreSQL dump was restored to a second database and reproduced every one of
those durable counts, including the recovery audit. Its SHA-256 is
`7fa6ce6809b8f0815c0d3a8f68043b1fc9e8deac4e57b1bbfc97409ecb209c1c`.
The 4.1 GiB temporary VM was then deleted; the dump, sanitized summary and
Compose overlay remain outside Git under
`$HOME/Library/Application Support/EvoAgent-Docker/app-recovery-acceptance-20260829`.
The main 8082 deployment stayed ready throughout.

This qualifies one forced local VM power-loss, automatic service restart,
PostgreSQL crash recovery, Redis-loss detection, offline intent reconstruction
and backup restore path. It does not establish a production RPO/RTO, storage
device loss, multi-node failover, hypervisor/VM suspend behavior, external-effect
reconciliation, representative load or a second VM fault during recovery.

### Context Pack and dual-axis workflow acceptance — 2026-08-29

The review input now carries a bounded `review-context@1` value containing the
change title, Spec and project standards. It is normalized at admission, included
in idempotency and workflow input identity, persisted with the task, restored on
resume and projected as readable sections rather than raw task JSON. GitHub PR
title/body ingestion uses the same contract with a server-owned origin and a
UTF-8-safe 32 KiB body limit. Context remains untrusted data: it cannot select
tools, permissions, tenants, models or workflow edges.

Studio exposes the context source port and a typed dual-axis template. Standards
and Spec analysis run in parallel, merge through the existing findings contract,
then reuse the critic, reproduction, synthesis, repair and verification chain.
When no model route is configured, the template remains visible but disabled
with an explicit reason; no unusable action is presented as available.

The deployable build-input fingerprint is
`e11efb5f474b410a87647d4e8b897c8ce8fe4a5639e33be1d51409efbe6b2e0d`.
Targeted backend checks passed **228 tests and 275 subtests**; all eight frontend
script groups passed. The full isolated regression passed **961 tests and 897
subtests, zero skipped** in **121.55 seconds**, including real PostgreSQL
backup/restore, Redis and container execution. Ruff, formatting and mypy over
65 source modules also passed. JUnit evidence is retained outside Git as
`context-pack-dual-axis-20260829-zero-skip.xml`.

The 8082 deployment now runs `evoagent:local-e11efb5f474b` and
`evoagent-proof:local-e11efb5f474b`; both are healthy with schema 28, Redis
workers 4/4, an empty Outbox and the private Proof socket. Browser acceptance
confirmed the floating navigation, page transition, disabled-template state and
all three Context fields. A real manual review stored the normalized Context and
reached `SUCCESS` through the existing published three-Agent flow. After the
sandbox image was pinned to the same fingerprint, a fresh frontend Proof reached
L4 with `failed/passed/passed` and left no labelled sandbox behind. This does not
qualify a live model provider or GitHub side effect; the typed dual-axis execution
is covered by the deterministic native-workflow test gateway.

### Repository Evidence Pack acceptance — 2026-08-29

Workflows can now opt into a bounded `repository-evidence@1` input. The worker
downloads the assigned GitHub head SHA only when the compiled workflow actually
connects `$input.evidence`, derives Python changed/caller/importer summaries with
the existing CodeGraph, and persists only that source-free summary. Manual Diff
reviews remain explicitly unavailable rather than implying repository access.
Archive or parse failure stores the unavailable result for deterministic retry
and continues with Diff and Context.

The deployable build-input fingerprint is
`e117c9003f5c5d718a1f441f5d180e9dbe855e655d348fcbc0fed68107c137d2`.
Targeted real PostgreSQL/Redis checks passed **337 tests and 387 subtests, zero
skipped**. The full isolated regression passed **971 tests and 902 subtests,
zero skipped** in **123.65 seconds**, including PostgreSQL backup/restore, Redis
and real container contracts. Ruff, formatting, mypy over 65 source modules and
all eight frontend script groups passed.

The 8082 deployment runs `evoagent:local-e117c9003f5c` and
`evoagent-proof:local-e117c9003f5c`; both are healthy with schema 28, Redis
workers 4/4, an empty Outbox and the private Proof socket. Browser acceptance
confirmed that Studio exposes the readable “仓库影响证据” source port, the
published three-Agent workflow still renders all typed handoffs, the dual-axis
template is visibly disabled while no model route exists, and both Studio and
Proof emit no console errors. A fresh frontend Proof reached L4 with
`failed/passed/passed` and left no `evoagent-verify-*` container behind.

This local environment has no configured GitHub App installation or model
route. Remote archive fetching and failure degradation are therefore qualified
by deterministic GitHub-adapter and real-database worker tests, not by a live
GitHub side effect; the UI projection is covered by frontend tests using the
same allowlisted artifact contract.

### Structured Agent Playbook qualification — 2026-08-29

Studio model Agents now author a bounded Playbook with separate identity,
objective and operating instructions. It remains part of the existing immutable
Agent JSON definition: the same version and SHA-256 digest also cover its model,
read-only tools, token limit and typed ports. The server compiles the Playbook
and appends the output-port schema and trust-boundary rules; authored text cannot
replace those contracts or grant permissions. No table or second runtime was
added. Legacy `prompt` definitions retain their exact shape and digest so pinned
workflows and failed tasks can resume without a silent instruction rewrite.

The build-input fingerprint is
`32a500a682bc8892dfea698fbd3c812e74b40e10b0296f9638f4daed967b0b2b`.
Real PostgreSQL Studio/workflow checks passed **101 tests and 144 subtests**.
The full isolated regression passed **972 tests and 906 subtests, zero skipped**
in **121.89 seconds**, including PostgreSQL backup/restore, Redis and container
contracts. Ruff, formatting, mypy over 65 source modules, JavaScript syntax and
all eight frontend script groups passed.

The 8082 deployment runs `evoagent:local-32a500a682bc` and
`evoagent-proof:local-32a500a682bc`; both are healthy with schema 28, workers
4/4, an empty Outbox and the private Proof socket. The working-tree and running
container SHA-256 values match for `evoagent/studio.py` and `web/studio.js`; the
served `/assets/studio.js` also matches the container exactly. An unauthenticated
browser reload confirmed the deployment's login gate without entering the saved
administrator password. UI acceptance then used a temporary loopback-only,
authentication-disabled process with a new PostgreSQL database, an empty Skill
directory, an in-process queue and no provider/GitHub/Proof credentials. It
confirmed the three Playbook fields, edited and saved a draft, reopened it with
all values preserved, kept draft saving enabled while model-dependent publish
and add actions were disabled, and emitted no browser console errors. The
temporary tab, process, database and directory were removed; 8082 stayed ready
and its persistent data was not touched. Authenticated RBAC remains covered by
the real-PostgreSQL HTTP tests. This environment has no live model route, so
actual provider execution is covered only by the deterministic governed-gateway
tests.

### Curated Agent recipe qualification — 2026-08-29

Studio now exposes three server-owned draft starters: standards and architecture,
Spec alignment, and regression/verification. They adapt the composable review,
deep-module, TDD and diagnostic-loop disciplines from the referenced Matt Pocock
Skill collection to EvoAgent's existing PR-review contracts. Selecting a recipe
copies a validated Agent definition into the normal editor. It does not persist a
recipe identifier, fetch remote Markdown or add another runtime, so future catalog
changes cannot alter published Agent versions, workflow bundles or pinned tasks.

The build-input fingerprint is
`b83a938c735411f5968287f38df8faba0fb2360dbeec4490b00dcca28016f4aa`.
Targeted real-PostgreSQL checks passed **22 tests and 75 subtests**; the full
isolated regression passed **972 tests and 906 subtests, zero skipped** in
**124.02 seconds**, including PostgreSQL backup/restore, Redis and container
contracts. Ruff, formatting, mypy over 65 source modules, shell/JavaScript syntax
and all eight frontend script groups passed.

The 8082 deployment runs `evoagent:local-b83a938c7354` and
`evoagent-proof:local-b83a938c7354`; schema 28, Redis workers 4/4, the empty
Outbox and the private Proof socket are healthy. Worktree/container hashes match
for `evoagent/studio.py` and `web/studio.js`. A separate loopback-only,
authentication-disabled process with a disposable PostgreSQL database, empty
Skill directory, memory queue and no provider/GitHub/Proof credentials confirmed
that all three recipe cards open the intended editable Playbook. The regression
recipe was edited, saved as a draft and reopened with all values preserved;
model-dependent publish/add stayed disabled while draft save remained available,
and the browser console stayed empty. The temporary tab and process were closed,
the disposable database and empty directory were removed, and 8082 remained
ready with no `evoagent-verify-*` containers.

### Repository Standards Pack qualification — 2026-08-29

When a pinned GitHub workflow requests Repository Evidence, the worker now uses
the same verified `head_sha` archive traversal to populate an otherwise empty
Context Pack standards section. The allowlist contains root `AGENTS.md`,
`CONTRIBUTING.md`, `CODING_STANDARDS.md`, `.github/CONTRIBUTING.md` and only the
ancestor `AGENTS.md` files that scope changed paths. Each included document is
labelled by repository path. Explicit caller standards win; unrelated paths,
arbitrary filenames and remote Markdown never enter the pack.

Standards share the existing 32 KiB UTF-8 section limit. Oversized prefixes are
cut on a valid character boundary; non-UTF-8, NUL and symlink documents are
omitted and mark the Context Pack truncated. Code impact and standards are
derived in one bounded archive pass, then Evidence and any Context enrichment
are atomically merged into the task before workflow execution. Resume reads that
snapshot and never substitutes a later repository revision. The content remains
untrusted data and cannot modify Playbooks, ports, tools, models, permissions or
workflow edges.

The build-input fingerprint is
`e2f3a25b84dee0a173bf76ed5150ac3d64ac682f21b876ced0ee27a7598e20b1`.
Targeted checks with real PostgreSQL passed **93 tests and 36 subtests**. The full
isolated regression passed **977 tests and 906 subtests, zero skipped** in
**125.72 seconds**, including PostgreSQL backup/restore, Redis and container
contracts. Ruff, formatting, mypy over 65 source modules, shell/JavaScript syntax
and all eight frontend script groups passed. The newly built application image
then passed the nine real container-integration tests and two subtests in
46.93 seconds, leaving no labelled sandbox behind.

The 8082 deployment runs `evoagent:local-e2f3a25b84de` and
`evoagent-proof:local-e2f3a25b84de`; schema 28, Redis workers 4/4, the empty
Outbox and the private Proof socket are healthy. Worktree/container hashes match
for `evoagent/codegraph.py` and `evoagent/application/reviews.py`. A disposable
loopback-only browser environment confirmed that file-labelled standards text is
accepted by the readable Context UI, Studio still exposes all three Agent recipes
and the Repository Evidence port, and the console contains no errors. The tab,
process, database and empty Skill directory were removed; 8082 stayed ready.
This environment has no live GitHub installation, so remote archive download is
qualified by deterministic code-host/application tests rather than a GitHub side
effect.

### Workflow and Agent observability qualification — 2026-08-29

The workflow runner now records bounded per-class Agent executions, successes,
failures, checkpoint reuses and latency histograms. The coordinator separately
records default, Studio and trusted-custom workflow outcomes and durations. Exact
user names, task/repository/tenant IDs and immutable revision hashes remain in the
tenant-scoped workflow checkpoint API rather than becoming Prometheus series.
The model gateway records provider-reported input/output tokens and request
failures under fixed review, Studio, evaluation and other purpose classes.

Targeted real PostgreSQL/Redis checks passed **82 tests and 168 subtests, zero
skipped**. The full suite passed **971 tests and 905 subtests** in **74.21 seconds**;
its nine opt-in Docker checks then passed separately with **2 subtests** in
**46.70 seconds** and left no sandbox container behind. The only unavailable
optional check was the existing backup/restore integration because this Mac has
no `pg_dump`/`pg_restore` client. Ruff, formatting, mypy over 65 source modules,
shell/JavaScript syntax and all eight frontend script groups passed. Prometheus
3.5.0 accepted all **39 rules** and the native alert tests, including the new
5% Agent-failure threshold, 20-execution sample floor and 10-minute pending time.

The build-input fingerprint is
`6787f2128c3a3b46f50eb81c773a515d210432dc63ad7850fcf8e143a7c05c82`.
The 8082 deployment runs `evoagent:local-6787f2128c3a` and
`evoagent-proof:local-6787f2128c3a`; schema 28, Redis workers 4/4, the empty
Outbox and private Proof socket are healthy. A real local review reached
`SUCCESS` with one finding and exported seven successful Agent executions plus
all seven per-role duration histograms. Worktree/container hashes match for the
workflow, coordinator, Studio and model-gateway modules. No live model route is
configured, so runtime token emission is qualified by the deterministic gateway
tests rather than a paid provider call.

### Task-level Agent timing qualification — 2026-08-29

The workflow runner now stores each committed Agent attempt's start time and
handler-plus-output-validation duration inside the existing checkpoint envelope.
The manage-authorized workflow API can inspect both values; the console view
allowlists only `duration_ms`, and the Task Center renders that value as readable
Chinese text on the corresponding handoff card. Queue wait, upstream wait and
final checkpoint persistence are intentionally outside this duration. Old,
pending or interrupted checkpoints may omit it, and a retry shows the latest
committed attempt rather than a fabricated cumulative time.

Targeted real-PostgreSQL checks passed **45 tests and 81 subtests, zero skipped**.
The full suite passed **972 tests and 905 subtests** in **73.72 seconds**; its nine
opt-in Docker checks then passed separately with **2 subtests** in **46.37
seconds** and left no sandbox container behind. The only other unavailable
optional check was the existing backup/restore integration because this Mac has
no `pg_dump`/`pg_restore` client. Ruff, formatting, mypy over 65 source modules,
shell/JavaScript syntax, all eight frontend script groups and the diff check
passed.

The build-input fingerprint is
`f570ebbec66f577ba88daa7b4711ad00ca0f69efab1ccb22dacb6ca9f0fb353d`.
The 8082 deployment runs `evoagent:local-f570ebbec66f` and
`evoagent-proof:local-f570ebbec66f`; schema 28, Redis workers 4/4, the empty
Outbox and private Proof socket are healthy. A real deployed review reached
`SUCCESS`; all seven workflow nodes contained a start time and a non-negative
duration, while its console projection omitted the start time and retained the
same durations. A separate loopback-only, authentication-disabled process with a
disposable PostgreSQL database and empty Skill directory then confirmed that the
Task Center displayed all seven handoff durations (1–4 ms) and emitted no browser
console errors. The temporary browser, process, database and directory were
removed; 8082 remained ready.

### Tenant operations console qualification — 2026-08-30

The browser now reuses the existing tenant-scoped audit, admission, Queue DLQ
and Outbox APIs in a role-gated Operations view. Auditors receive only actor,
action, resource and time. Admins additionally receive capacity counters, Queue
task IDs and the dead Outbox identifier required for explicit replay. Console
transport projections omit audit detail, queue/Outbox payloads and stored error
text; the request header does not elevate the existing `audit` or `manage`
authorization checks. Outbox replay retains the existing dead-row transaction,
dispatcher notification and actor audit rather than adding a second recovery
path. Queue execution retries continue through Task Center.

Targeted checks with real PostgreSQL passed **20 tests and 31 subtests, zero
skipped**. The full real-PostgreSQL/Redis suite passed **974 tests and 905
subtests** in **74.72 seconds**; its nine opt-in Docker checks then passed with
**2 subtests** in **46.84 seconds** and left no sandbox container behind. The
only other unavailable optional check was the existing backup/restore integration
because this Mac has no configured PostgreSQL client tools. Ruff, formatting,
mypy over 65 source modules, shell/JavaScript syntax, all eight frontend script
groups and the diff check passed. The broken `.venv/bin/mypy` shebang points to a
deleted local interpreter, so the same installed mypy package was invoked through
the current `.venv/bin/python` and completed successfully.

The reproducible build-input fingerprint is
`e3b66c36c483897cd3520552e1979b7688023ce9e96187e64a7590d059ee9836`.
The 8082 deployment runs `evoagent:local-e3b66c36c483` and
`evoagent-proof:local-e3b66c36c483`; schema 28, Redis workers 4/4, the empty
Outbox and private Proof socket are healthy. Worktree/container SHA-256 values
match for the console projection, capability service and all three changed web
assets; the served `app.js` also matches. A separate loopback-only,
authentication-disabled process with a disposable PostgreSQL database and empty
Skill directory displayed capacity 0/4, the review audit and one seeded dead
Outbox item. Browser confirmation replayed only that delivery, reduced the dead
count to zero, added the readable `outbox.replay` audit event and emitted no
console errors. The tab, process, two disposable databases, temporary Redis and
empty directory were removed; 8082 remained ready.

### Repository governance console qualification — 2026-08-30

The browser now reads the current tenant/repository policy before enabling edits
and sends that exact version on every save. PostgreSQL compares the version under
the existing repository-policy lock; a stale write returns `409` before policy
history or audit data changes. The console projection exposes only the effective
policy, bounded version/actor/time history and installed reviewer/fix-rule names.
Tenant identifiers, historical policy bodies, credentials, metadata and raw JSON
remain outside the page.

The full real-PostgreSQL/Redis suite passed **977 tests and 910 subtests** in
**76.39 seconds**; its only skips were the nine separately qualified container
checks and the existing backup/restore check without configured PostgreSQL client
tools. The newly built image then passed all **9 real container-integration
tests** in **46.41 seconds** and left no sandbox container behind. Ruff,
formatting, mypy over 65 source modules, shell/JavaScript syntax, all nine
frontend tests and the diff check passed.

The reproducible build-input fingerprint is
`42010576fccbc7c0b0202502729cdf6a50626b3dd60a7bf51c3d36f73d95d5ee`.
The 8082 deployment runs `evoagent:local-42010576fccb` and
`evoagent-proof:local-42010576fccb`; schema 28, Redis workers 4/4, the empty
Outbox and private Proof socket are healthy. The application and executor image
IDs are `sha256:24ccb507c8a7ad78cb6954d5d773e9aadefe32bc0fee999dbe71a16966087f31`
and `sha256:e8e61ce77fda7ad843c414cd15e49cd591079e7274a0191d8e68e2566e34a736`.

A disposable loopback-only, authentication-disabled browser environment read a
new repository at v0, saved v1 and reread the persisted normalized policy. It
rendered readable summary/history cards and no browser errors. An external v2
write then made the browser's v1 edit stale: the attempted save received `409`,
kept the unsaved `65536`-byte limit and auto-fix edit visible, disabled further
writes, and required an explicit reread. Rereading showed v2 and both history
entries without raw JSON or tenant metadata. The temporary tab, process,
PostgreSQL database and Redis databases 14/15 were removed; 8082 stayed ready.

### Fixed PR comparison qualification — 2026-08-30

New GitHub webhook tasks now validate and persist both `pull_request.base.sha`
and `pull_request.head.sha` before the task/outbox transaction commits. The
worker uses GitHub's immutable `BASE...HEAD` comparison endpoint, then stores the
bounded Diff, byte count and SHA-256 before review execution. A missing pinned
comparison is retried or failed without reading the current PR. Existing tasks
created before this contract remain resumable through their stored Diff URL.

The GitHub adapter test verifies the exact compare path, diff media type,
authorization header and response over a real loopback HTTP connection. The
application/PostgreSQL test verifies that the task snapshot contains both SHAs,
the mutable URL is not called, a duplicate delivery reuses the persisted Diff,
and both pinned and legacy deferred tasks remain resumable. No live GitHub
installation is configured, so the provider-side call is not made during local
qualification.

The final real-PostgreSQL/Redis suite passed **979 tests and 919 subtests** in
**74.19 seconds**. A prior repeat exposed a Redis reconnect health race: delivery
could complete just before the previous socket error was cleared. Clearing that
stale dependency error immediately after a successful `XREADGROUP` fixed the
root ordering issue; the original fault test then passed **10 consecutive
runs**. The final image passed all **9 real container-integration tests** in
**46.33 seconds**. The only unavailable checks were those nine opt-in tests in
the main run and backup/restore without configured PostgreSQL client tools.
Ruff, formatting, mypy over 65 source modules, shell/JavaScript syntax, all nine
frontend tests and the diff check passed.

The reproducible build-input fingerprint is
`0576753be742f6a234091d4bd1d0e874ac2862c17c6f1829d2980cbfac8febfc`.
The 8082 deployment runs `evoagent:local-0576753be742` and
`evoagent-proof:local-0576753be742`; schema 28, Redis workers 4/4, the empty
Outbox and private Proof socket are healthy. The application and executor image
IDs are `sha256:c27c0ba76699faced26b201a7769251c381804b3ecd7062ee0681543b9e7a65f`
and `sha256:31244bce1edaf274a5708ba2314a27be720ec24d1f9c71a534ddc4e4c96c5cdf`.
Worktree/container hashes match for the GitHub adapter, webhook/review use cases
and Redis queue. The disposable PostgreSQL database and Redis test database were
removed after qualification; 8082 stayed ready.

### Dynamic Skill pipe lifecycle qualification — 2026-08-30

The bounded Skill runner now makes each drain thread close the stdout/stderr
pipe it owns after normal EOF, output-limit termination or timeout termination.
This keeps the existing bounded-reader and process-group cleanup model while
removing the repeated file-descriptor leak observed during real container runs.
No polling loop, dependency or alternate executor was added.

The focused Skill suite passed **41 tests and 47 subtests** with
`ResourceWarning` promoted to an error. Its explicit regression runs normal,
over-limit and timeout subprocesses, forces garbage collection and verifies that
no pipe warning remains. The full real-PostgreSQL/Redis suite passed **980 tests
and 922 subtests** in **73.45 seconds**. The final image then passed all **9 real
container-integration tests** in **46.15 seconds** with `ResourceWarning`
promoted to an error and no warning output. Ruff, formatting, mypy over 65 source
modules, shell/JavaScript syntax, all nine frontend tests and the diff check
passed.

The reproducible build-input fingerprint is
`b76b49de21189911f6ef946ddeada873e9f46fa9d88e88974c1896bbbbb06685`.
The 8082 deployment runs `evoagent:local-b76b49de2118` and
`evoagent-proof:local-b76b49de2118`; schema 28, Redis workers 4/4, the empty
Outbox and private Proof socket are healthy. The application and executor image
IDs are `sha256:72d25bc66701dab6c8e29b1aca5177f69bf0c280e3eaf0f635f8bc99b5631e21`
and `sha256:cf8de5884f8228153e1ee85cba7dbd1d5509ca27405c8a9662ef1d5fd5ba239e`.
The worktree and deployed `evoagent/skills.py` hashes match. The disposable
PostgreSQL database and Redis test database were removed; 8082 stayed ready.
