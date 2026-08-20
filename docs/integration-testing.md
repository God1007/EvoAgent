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
- Redis ACK, dedupe, heartbeat, reclaim, retry and DLQ replay;
- local container Proof and repair verification;
- PostgreSQL restore drills and PostgreSQL-to-Redis reconstruction;
- wheel installation and console entry points.

Tests must use disposable databases. `tests/db_support.py` creates an isolated
schema per test and removes it afterward; do not point it at production.
