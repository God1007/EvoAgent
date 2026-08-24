import hashlib
import json
import threading
import unittest
from unittest import mock

from evoagent.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationApplyError,
    SchemaHistoryError,
    SchemaTooNewError,
    migrate_postgres,
)
from evoagent.postgres_store import PostgresTaskStore
from evoagent.time_utils import utc_now
from tests.db_support import postgres_url


class MigrationCatalogTests(unittest.TestCase):
    def test_released_migration_checksums_remain_explicit_and_current(self):
        self.assertEqual(
            list(range(1, CURRENT_SCHEMA_VERSION + 1)), [m.version for m in MIGRATIONS]
        )
        self.assertTrue(all(len(m.checksum) == 64 for m in MIGRATIONS))
        # Versions 1-14 retain checksums from the retired dual-database catalog.
        for migration in MIGRATIONS[14:]:
            payload = json.dumps(
                {
                    "version": migration.version,
                    "name": migration.name,
                    "postgres": migration.statements,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.assertEqual(migration.checksum, hashlib.sha256(payload.encode()).hexdigest())

    def test_store_schema_version_revalidates_live_history(self):
        current = [
            {"version": item.version, "name": item.name, "checksum": item.checksum}
            for item in MIGRATIONS
        ]
        connection = mock.Mock()
        connection.execute.return_value.fetchall.side_effect = [
            current,
            [*current, {"version": CURRENT_SCHEMA_VERSION + 1, "name": "future", "checksum": "x"}],
        ]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = PostgresTaskStore.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)  # type: ignore[method-assign]

        self.assertEqual(CURRENT_SCHEMA_VERSION, store.schema_version())
        with self.assertRaises(SchemaTooNewError):
            store.schema_version()

        self.assertEqual(2, store._connect.call_count)

    def test_migration_failure_does_not_copy_database_error_text(self):
        connection = mock.Mock()
        connection.execute.side_effect = RuntimeError("password=database-secret")

        with self.assertRaises(MigrationApplyError) as raised:
            migrate_postgres(connection)

        self.assertIn("RuntimeError", str(raised.exception))
        self.assertNotIn("database-secret", str(raised.exception))


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
            session_columns = {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='review_sessions'"
                ).fetchall()
            }
            indexes = {
                row["indexname"]
                for row in conn.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"
                ).fetchall()
            }
        self.assertIn("tasks", tables)
        self.assertIn("outbox_messages", tables)
        self.assertIn("consumed_auth_states", tables)
        self.assertNotIn("model_usage", tables)
        self.assertNotIn("model_route_shadows", tables)
        self.assertNotIn("model_route_capacity_leases", tables)
        self.assertIn("last_webhook_at", session_columns)
        self.assertIn("idx_evolution_runs_skill_candidate_created", indexes)
        self.assertIn("idx_effect_receipts_completed_at", indexes)
        self.assertIn("idx_webhook_deliveries_received_at", indexes)

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
        with self.assertRaises(SchemaTooNewError):
            store.schema_version()
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

    def test_deployment_invariants_are_enforced_by_postgresql(self):
        import psycopg

        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0)
        self.addCleanup(store.close)
        store.save_deployment(
            "tenant-a",
            "review-skill",
            {
                "stable_version": 1,
                "candidate_version": 2,
                "status": "running",
            },
        )

        with self.assertRaises(psycopg.errors.CheckViolation):
            with store._connect() as conn:
                conn.execute(
                    "UPDATE deployments SET errors=samples+1 "
                    "WHERE tenant_id='tenant-a' AND skill_name='review-skill'"
                )

    def test_old_writer_cannot_activate_an_unqualified_skill_version(self):
        import psycopg

        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0)
        self.addCleanup(store.close)

        with self.assertRaises(psycopg.errors.CheckViolation):
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO skill_versions(skill_name,version,prompt,score,active,"
                    "parent_version,created_at) VALUES (%s,1,%s,0,TRUE,NULL,%s)",
                    ("llm-review", "unqualified", utc_now()),
                )

    def test_qualification_migration_preserves_evolution_decisions(self):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            migrate_postgres(conn, 17)
            for version, active in ((1, True), (2, False), (3, False), (4, False)):
                conn.execute(
                    "INSERT INTO skill_versions(skill_name,version,prompt,score,active,"
                    "parent_version,created_at) VALUES (%s,%s,%s,0,%s,NULL,%s)",
                    ("llm-review", version, "prompt-%s" % version, active, utc_now()),
                )
            for version, decision in ((1, "activated"), (2, "rejected"), (4, "activated")):
                conn.execute(
                    "INSERT INTO evolution_runs(id,skill_name,candidate_version,baseline_version,"
                    "decision,candidate_score,baseline_score,metrics_json,created_at) "
                    "VALUES (%s,%s,%s,NULL,%s,0,0,'{}'::jsonb,%s)",
                    ("run-%s" % version, "llm-review", version, decision, utc_now()),
                )

            migrate_postgres(conn)
            rows = conn.execute(
                "SELECT version,qualification FROM skill_versions ORDER BY version"
            ).fetchall()

        self.assertEqual(
            [(1, "legacy"), (2, "rejected"), (3, "rejected"), (4, "approved")],
            [(row["version"], row["qualification"]) for row in rows],
        )
