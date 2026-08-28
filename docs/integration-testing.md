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

### Remaining release qualification

The architectural and handoff evidence above does not establish a production
release. On 2026-08-28, the [main CI run](https://github.com/God1007/EvoAgent/actions/runs/33100828640)
passed for `46927e5424379169dbe2386874d505a46fe6084c`; that snapshot does not
qualify later commits. Check each proposed release's exact commit in Actions.
Local container isolation checks remain unexecuted, and live
provider/GitHub behavior and deployment SLOs are not qualified. The controlled
evaluation still fails repair-coverage and independent-data provenance gates.
The [performance audit](performance.md) records the corrected generator timing
and accounting, real HTTP failure/backlog checks, and a controlled three-Agent
completion/replica comparison with reconciled task and checkpoint counts. Representative
end-to-end Agent load and deployment SLO qualification still remain; local
toolchain checks do not replace them. These are outstanding requirements, not
waived gates.
