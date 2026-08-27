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

Application source SHA-256: `5c6b41daaf8a289d7fb238d77fbaaf41ff169fa31e078079b763fc36c107fb5f`
(the same revision as the [controlled evaluation](evaluation-baseline.md)).

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

### Remaining release qualification

The architectural and handoff evidence above does not establish a production
release. On 2026-08-28, the [main CI run](https://github.com/God1007/EvoAgent/actions/runs/32702890236)
passed for `150c4324f69a4f4aef925c8db4940c98f5537f67`; current uncommitted changes
have not run there. Local container isolation checks remain unexecuted, and live
provider/GitHub behavior and deployment SLOs are not qualified. The controlled
evaluation still fails repair-coverage and independent-data provenance gates.
The [performance audit](performance.md) records the corrected generator timing
and accounting, including real HTTP failure/backlog checks. Representative
end-to-end Agent load and deployment SLO qualification still remain; local
toolchain checks do not replace them. These are outstanding requirements, not
waived gates.
