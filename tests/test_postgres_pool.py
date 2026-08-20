import unittest
from contextlib import contextmanager

from evoagent.metrics import Metrics
from evoagent.postgres_store import PostgresTaskStore, create_store


class FakeConn:
    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql, *args):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return {"database_name": "evoagent"}


class PoolExhausted(RuntimeError):
    pass


class FakePool:
    def __init__(self):
        self.checked_out = 0
        self.max_concurrent = 0
        self.closed = False
        self.exhaust = False

    @contextmanager
    def connection(self, timeout=None):
        if self.exhaust:
            raise PoolExhausted("pool timeout")
        self.checked_out += 1
        self.max_concurrent = max(self.max_concurrent, self.checked_out)
        try:
            yield FakeConn()
        finally:
            self.checked_out -= 1  # returned to pool, not closed

    def get_stats(self):
        return {"pool_size": 5, "pool_available": 3, "requests_waiting": 1}

    def close(self):
        self.closed = True


class FakePsycopg:
    def __init__(self):
        self.connect_calls = 0
        self.sentinel = object()

    def connect(self, url, row_factory=None):
        self.connect_calls += 1
        return self.sentinel


def _store_with_pool(pool):
    store = object.__new__(PostgresTaskStore)
    store.psycopg = FakePsycopg()
    store.dict_row = object()
    store.url = "postgresql://example/db"
    store.pool_timeout = 5.0
    store._pool = pool
    return store


class PoolCheckoutTests(unittest.TestCase):
    def test_connect_checks_out_and_returns_to_pool(self):
        pool = FakePool()
        store = _store_with_pool(pool)
        with store._connect() as conn:
            self.assertIsInstance(conn, FakeConn)
            self.assertEqual(1, pool.checked_out)
        self.assertEqual(0, pool.checked_out)  # returned, not leaked/closed

    def test_ping_uses_pool(self):
        pool = FakePool()
        store = _store_with_pool(pool)
        store.ping()
        self.assertEqual(0, pool.checked_out)

    def test_database_confirmation_uses_server_reported_identity(self):
        store = _store_with_pool(FakePool())
        self.assertEqual("evoagent", store.connected_database_name())

    def test_exhaustion_timeout_propagates(self):
        pool = FakePool()
        pool.exhaust = True
        store = _store_with_pool(pool)
        with self.assertRaises(PoolExhausted):
            with store._connect():
                pass

    def test_pool_stats_exposed(self):
        store = _store_with_pool(FakePool())
        stats = store.pool_stats()
        self.assertEqual(5, stats["pool_size"])
        self.assertEqual(3, stats["pool_available"])

    def test_close_closes_pool(self):
        pool = FakePool()
        store = _store_with_pool(pool)
        store.close()
        self.assertTrue(pool.closed)


class FallbackTests(unittest.TestCase):
    def test_connect_falls_back_to_per_call_connection(self):
        store = _store_with_pool(None)
        result = store._connect()
        self.assertIs(store.psycopg.sentinel, result)
        self.assertEqual(1, store.psycopg.connect_calls)

    def test_pool_stats_none_without_pool(self):
        store = _store_with_pool(None)
        self.assertIsNone(store.pool_stats())

    def test_create_store_rejects_non_postgres_url(self):
        with self.assertRaisesRegex(ValueError, "PostgreSQL URL"):
            create_store("")


class PoolStatGaugeTests(unittest.TestCase):
    def test_stat_reader_probes_alternate_keys(self):
        # Mirrors service._register_pool_metrics: tolerate version key drift.
        m = Metrics()
        store = _store_with_pool(FakePool())

        def stat(*keys):
            def read():
                current = store.pool_stats() or {}
                for key in keys:
                    if key in current:
                        return float(current[key])
                return 0.0

            return read

        m.register_gauge_source("pg_pool_waiting", stat("requests_waiting", "requests_queued"))
        self.assertIn("evoagent_pg_pool_waiting 1", m.prometheus())


if __name__ == "__main__":
    unittest.main()
