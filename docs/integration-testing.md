# External integration test matrix

The ordinary test suite is deterministic and works without infrastructure. The
CI `external-integration` job is a separate mandatory boundary suite: skipped
external tests become active because the job always provisions PostgreSQL 16,
standalone Redis 7, a three-primary Redis 7 Cluster, and Docker.

## What the matrix proves

| Boundary | Required evidence |
| --- | --- |
| PostgreSQL schema | Real migration CLI reaches the current checksummed version and is idempotent |
| PostgreSQL adapter | The same behavior contract as SQLite passes against the real driver |
| PostgreSQL pool | Checkout timeout is bounded; a server-terminated connection is replaced |
| Redis publication | Stable message IDs deduplicate across queue object/process restart |
| Redis consumption | A process dies after `XREADGROUP`; another consumer reclaims after the lease |
| Redis tenant fairness | Uniform and weighted tenant backlogs rotate handler starts through atomic cross-replica scheduler state; retries and reclaimed admissions leave no waiting/index leak |
| Redis live lease | A handler that runs beyond the static reclaim lease renews its pending idle time and is not executed concurrently by another consumer |
| Redis Cluster | A real three-primary Redis 7 cluster proves topology discovery, same-slot atomic publish/dedupe/ACK, weighted turns, live lease renewal, incompatible protocol rejection, and namespace-scoped recovery |
| Redis recovery | A disconnected client socket reconnects without permanently losing its worker |
| Redis DLQ | Permanent failure survives queue restart and explicit replay succeeds |
| GitHub transport | Auth/version headers, retry response, JSON body, and PATCH upsert cross a real HTTP socket |
| Verifier | Real Docker run has no network, read-only root, bounded resources, no ambient host secret, and timeout cleanup |
| Remote Proof Runner | Signed loopback HTTP reaches a real netless job container and returns content-addressed input/evidence attestations without leaking an ambient secret |
| PostgreSQL recovery | One exported MVCC snapshot is dumped, restored only into a generated database, compared table-by-table, exercised through the Store adapter, and removed within RPO/RTO |
| Queue reconstruction | A previously published incomplete task is planned, audited, restaged, and published through the production Outbox/TaskQueue path only after reserving an empty Redis logical database or v2 Cluster namespace |
| Evaluation evidence | Answer-free cases, blind dual annotations, independent adjudication, packet hashes, provenance sidecar, slice metrics, and fail-closed tamper cases run in the normal quality suite |
| Distribution | The installed wheel serves packaged web assets and contains the bundled Skill |
| Production image | `/ready` sees Postgres, Redis workers, lease heartbeat, enabled fair scheduling, schema and Outbox; async review reaches `SUCCESS`; SIGTERM exits cleanly |

## Local reproduction

Use dedicated disposable databases. The tests delete EvoAgent's Redis test keys
and create persistent PostgreSQL rows with unique identifiers; never point them
at shared staging or production infrastructure.

```bash
docker compose up -d postgres redis

export EVOAGENT_TEST_POSTGRES_URL=postgresql://evoagent:evoagent-local@127.0.0.1:5432/evoagent
export EVOAGENT_TEST_REDIS_URL=redis://127.0.0.1:6379/0

python -m pytest -q \
  tests/test_adapter_contracts.py \
  tests/test_external_integrations.py \
  tests/test_github_http_fixture.py

export EVOAGENT_DATABASE_URL="$EVOAGENT_TEST_POSTGRES_URL"
evoagent-dr --backend postgresql \
  --output-dir /tmp/evoagent-recovery \
  --max-rpo-seconds 300 \
  --max-rto-seconds 300

docker build -t evoagent:integration .
EVOAGENT_TEST_CONTAINER_IMAGE=evoagent:integration \
  python -m pytest -q tests/test_container_integration.py
```

The recovery command requires PostgreSQL client tools and a disposable database
role with `CREATEDB`; it never restores into the named source database. The
workflow itself is the executable source of truth for image startup and the
full-stack Outbox path. A local machine without Docker/PostgreSQL/Redis can still
run `make check`; its report must retain the explicit external-test skips rather
than being represented as production-backend evidence.
