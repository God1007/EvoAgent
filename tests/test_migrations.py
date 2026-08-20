import threading
import unittest

from evoagent.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    SchemaHistoryError,
    SchemaTooNewError,
)
from evoagent.postgres_store import PostgresTaskStore
from evoagent.time_utils import utc_now
from tests.db_support import postgres_url


class PostgreSQLMigrationTests(unittest.TestCase):
    def setUp(self):
        self.url = postgres_url(self)
        self._drop_application_tables()

    def tearDown(self):
        self._drop_application_tables()

    def _drop_application_tables(self):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
            ).fetchall()
            if rows:
                sql = psycopg.sql
                tables = sql.SQL(", ").join(sql.Identifier(row["table_name"]) for row in rows)
                conn.execute(sql.SQL("DROP TABLE {} CASCADE").format(tables))

    def test_clean_database_reaches_current_schema(self):
        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0)
        self.addCleanup(store.close)

        self.assertEqual(CURRENT_SCHEMA_VERSION, store.schema_version())
        with store._connect() as conn:
            tables = {
                row["table_name"]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=current_schema()"
                ).fetchall()
            }
        self.assertIn("tasks", tables)
        self.assertIn("outbox_messages", tables)
        self.assertNotIn("model_usage", tables)
        self.assertNotIn("model_route_shadows", tables)
        self.assertNotIn("model_route_capacity_leases", tables)

    def test_tampered_history_fails_closed(self):
        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0)
        with store._connect() as conn:
            conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
        store.close()

        with self.assertRaises(SchemaHistoryError):
            PostgresTaskStore(self.url, pool_min=0, pool_max=0)

    def test_newer_schema_fails_closed(self):
        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0)
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (%s,%s,%s,%s)",
                (CURRENT_SCHEMA_VERSION + 1, "future", "future", utc_now()),
            )
        store.close()

        with self.assertRaises(SchemaTooNewError):
            PostgresTaskStore(self.url, pool_min=0, pool_max=0)

    def test_concurrent_startup_serializes_migrations(self):
        versions = []
        failures = []

        def start():
            try:
                store = PostgresTaskStore(self.url, pool_min=0, pool_max=0)
                versions.append(store.schema_version())
                store.close()
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=start) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        self.assertEqual([], failures)
        self.assertEqual([CURRENT_SCHEMA_VERSION] * 4, sorted(versions))

    def test_released_migration_checksums_remain_explicit(self):
        self.assertEqual(
            list(range(1, CURRENT_SCHEMA_VERSION + 1)), [m.version for m in MIGRATIONS]
        )
        self.assertTrue(all(len(m.checksum) == 64 for m in MIGRATIONS))
