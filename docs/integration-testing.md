# Integration testing

Local unit tests run without external services. Database tests require an
explicit temporary PostgreSQL URL and otherwise skip; Redis and container tests
follow the same opt-in pattern.

```bash
export EVOAGENT_TEST_POSTGRES_URL='postgresql://evoagent:password@127.0.0.1:5432/evoagent_test'
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

Tests must use disposable databases. `tests/db_support.py` truncates application
tables between tests, so database cases must not run in parallel against the
same database; never point it at production. CI also fails if its selected
PostgreSQL, Redis, or container contract suites skip any test. The real-service
job retains JUnit results and PostgreSQL-adapter coverage XML for boundary-level
evidence and future coverage-floor calibration.
