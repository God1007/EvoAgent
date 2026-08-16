# External integration test matrix

The ordinary test suite is deterministic and works without infrastructure. The
CI `external-integration` job is a separate mandatory boundary suite: skipped
external tests become active because the job always provisions PostgreSQL 16,
Redis 7, and Docker.

## What the matrix proves

| Boundary | Required evidence |
| --- | --- |
| PostgreSQL schema | Real migration CLI reaches the current checksummed version and is idempotent |
| PostgreSQL adapter | The same behavior contract as SQLite passes against the real driver |
| PostgreSQL pool | Checkout timeout is bounded; a server-terminated connection is replaced |
| Redis publication | Stable message IDs deduplicate across queue object/process restart |
| Redis consumption | A process dies after `XREADGROUP`; another consumer reclaims after the lease |
| Redis recovery | A disconnected client socket reconnects without permanently losing its worker |
| Redis DLQ | Permanent failure survives queue restart and explicit replay succeeds |
| GitHub transport | Auth/version headers, retry response, JSON body, and PATCH upsert cross a real HTTP socket |
| Verifier | Real Docker run has no network, read-only root, bounded resources, no ambient host secret, and timeout cleanup |
| Distribution | The installed wheel serves packaged web assets and contains the bundled Skill |
| Production image | `/ready` sees Postgres, Redis workers, schema and Outbox; async review reaches `SUCCESS`; SIGTERM exits cleanly |

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

docker build -t evoagent:integration .
EVOAGENT_TEST_CONTAINER_IMAGE=evoagent:integration \
  python -m pytest -q tests/test_container_integration.py
```

The workflow itself is the executable source of truth for image startup and the
full-stack Outbox path. A local machine without Docker/PostgreSQL/Redis can still
run `make check`; its report must retain the explicit external-test skips rather
than being represented as production-backend evidence.
