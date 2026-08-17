# Adapter ports and contract tests

EvoAgent separates two related extension concepts:

- a **Capability** identifies which replaceable service a trusted plugin
  provides at composition time;
- a **Port** defines the behavior that service must expose to the application
  and domain at type-check and test time.

The stable port definitions live in `evoagent/ports.py`. Infrastructure
adapters use structural Python Protocols, so an external provider does not have
to inherit an EvoAgent implementation class.

## Port map

| Port | Consumer | Built-in adapters |
| --- | --- | --- |
| `ApplicationStorePort` | application facade and API | `TaskStore`, `PostgresTaskStore` |
| `ReviewExecutionStorePort` | checkpointed review harness | SQLite/PostgreSQL store facet |
| `ReviewWorkflowStorePort` | reviewer graph and agent messages | SQLite/PostgreSQL store facet |
| `AuthStorePort` | authentication and membership lookup | SQLite/PostgreSQL store facet |
| `EvolutionStorePort` | evaluation and skill version governance | SQLite/PostgreSQL store facet |
| `ReleaseStorePort` | canary, shadow, promotion, rollback | SQLite/PostgreSQL store facet |
| `AlertStorePort` | operational alert evaluation | SQLite/PostgreSQL store facet |
| `OutboxStorePort` | durable queue publication and recovery | SQLite/PostgreSQL store facet |
| `ReviewApplicationStorePort` | review task lifecycle and external-effect receipts | SQLite/PostgreSQL store facet |
| `SessionApplicationStorePort` | PR continuity and source impact | SQLite/PostgreSQL store facet |
| `RepairApplicationStorePort` | idempotent repair publication | SQLite/PostgreSQL store facet |
| `WebhookApplicationStorePort` | atomic delivery/session/task/outbox intake | SQLite/PostgreSQL store facet |
| `TaskQueuePort` / `QueueFactoryPort` | asynchronous review delivery | memory executor, standalone/Cluster Redis Streams |
| `TenantFairQueuePort` / `QueueTopologyPort` | optional scheduling and deployment telemetry | built-in queue; legacy providers remain compatible with neutral metrics |
| `CodeHostPort` | diff intake, comment delivery, repair PR | GitHub |

`ApplicationStorePort` is the composition-root capability. Lower-level modules
receive only the focused facet they consume. For example, `ReviewHarness`
cannot accidentally start depending on audit, authentication, or release
tables because its constructor accepts `ReviewExecutionStorePort`.

## Contract suite

`tests/test_adapter_contracts.py` contains two levels of checks:

1. runtime-checkable surface tests catch missing methods without contacting an
   external service;
2. one behavior suite is inherited by each adapter backend, proving task,
   checkpoint, session, identity, webhook, audit, shadow-release, delivery,
   health, shutdown, Outbox, and effect-receipt semantics. Webhook contracts
   include concurrent duplicate admission and injected transaction rollback.

SQLite and the memory queue always run in the local test suite. Production
backend contracts are enabled with:

```bash
export EVOAGENT_TEST_POSTGRES_URL=postgresql://evoagent:evoagent@localhost:5432/evoagent
export EVOAGENT_TEST_REDIS_URL=redis://localhost:6379/0
export EVOAGENT_TEST_REDIS_CLUSTER_URL=redis://localhost:7000/0
pytest -q tests/test_adapter_contracts.py
pytest -q tests/test_external_integrations.py
```

The tests use unique logical identifiers and do not require a clean database,
but the configured targets must be dedicated test infrastructure. The
PostgreSQL adapter closes its connection pool after each contract test.

The mandatory GitHub Actions matrix supplies both URLs on every pull request.
It additionally proves real pool timeout/replacement, Redis connection recovery,
consumer-process lease reclaim, durable DLQ replay, acknowledged Stream cleanup,
and a real three-primary Cluster with same-slot v2 keys. See
`integration-testing.md` for the full boundary map.

The initial contract extraction found a concrete parity defect: PostgreSQL did
not implement the shadow-observation/automatic-promotion behavior already
available in SQLite. The PostgreSQL schema and adapter now expose the same
release behavior, and the shared contract prevents that path from drifting
silently again.

## Adding an adapter

A trusted provider for `STORE`, `QUEUE_FACTORY`, or `GITHUB_CLIENT` should:

1. implement the corresponding Protocol without importing `ReviewService`;
2. preserve tenant filtering, idempotency, checkpoint, and lifecycle semantics;
3. add a subclass of the relevant behavior contract using a dedicated test
   backend;
4. pass Ruff, mypy, the local suite, and the production adapter integration
   matrix;
5. document durability, consistency, retry, cleanup, and credential boundaries.

Protocol conformance proves the callable surface; it does not prove transaction
isolation, durability, or recovery. Those guarantees require the behavior and
fault-injection suites described in the enterprise roadmap.
