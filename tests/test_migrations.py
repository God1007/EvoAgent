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

    def test_checkpoint_json_migration_preserves_history_and_new_numeric_values(self):
        import psycopg
        from psycopg.rows import dict_row

        state = {
            "outputs": {"negative_zero": -0.0, "large_float": 1e20},
            "output_sha256": "legacy-digest",
        }
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            migrate_postgres(conn, 27)
            conn.execute(
                "INSERT INTO tasks(id,state,repository,input_json,created_at,updated_at) "
                "VALUES ('legacy','EXECUTING','demo/repo','{}',%s,%s)",
                (utc_now(), utc_now()),
            )
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,updated_at) "
                "VALUES ('legacy','workflow:first','completed',3,%s::jsonb,%s)",
                (json.dumps(state), utc_now()),
            )
            before = conn.execute("SELECT * FROM checkpoints").fetchone()
            before_json = json.dumps(before["state_json"], sort_keys=True)
            self.assertNotEqual(json.dumps(state, sort_keys=True), before_json)
            migrate_postgres(conn)
            self.assertEqual(
                "json",
                conn.execute(
                    "SELECT pg_typeof(state_json)::text AS kind FROM checkpoints"
                ).fetchone()["kind"],
            )
            after = conn.execute("SELECT * FROM checkpoints").fetchone()
            self.assertEqual(before, after)
            self.assertEqual(before_json, json.dumps(after["state_json"], sort_keys=True))
            migrate_postgres(conn)
            self.assertEqual(
                1, conn.execute("SELECT count(*) AS n FROM checkpoints").fetchone()["n"]
            )
        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0, auto_migrate=False)
        self.addCleanup(store.close)
        store.save_checkpoint("legacy", "workflow:second", state)
        saved = store.load_checkpoints("legacy", "workflow:second")["workflow:second"]["state"]
        self.assertEqual(json.dumps(state, sort_keys=True), json.dumps(saved, sort_keys=True))
        # Migration cannot recover values already normalized by JSONB, and must not
        # replace an old digest or overwrite a completed first-write-wins record.
        store.save_checkpoint("legacy", "workflow:first", state, attempt=99)
        with store._connect() as conn:
            self.assertEqual(
                before,
                conn.execute("SELECT * FROM checkpoints WHERE node='workflow:first'").fetchone(),
            )

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

    def test_studio_binding_migration_pins_versions_and_fails_closed_on_invalid_legacy_rows(self):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            migrate_postgres(conn, 26)
            for tenant, versions in (("tenant-a", (1, 2)), ("tenant-b", (3,))):
                conn.execute(
                    "INSERT INTO studio_documents(tenant_id,kind,id,revision,definition_json,updated_at) "
                    "VALUES (%s,'workflows','flow',2,'{}',%s)",
                    (tenant, utc_now()),
                )
                for version in versions:
                    conn.execute(
                        "INSERT INTO studio_versions(tenant_id,kind,document_id,version,draft_revision,"
                        "definition_json,digest,created_at) VALUES (%s,'workflows','flow',%s,%s,'{}','fixture',%s)",
                        (tenant, version, version, utc_now()),
                    )
            conn.execute(
                "INSERT INTO studio_bindings(tenant_id,repository,workflow_id,updated_at) "
                "VALUES ('tenant-a','demo/repo','flow',%s)",
                (utc_now(),),
            )
        for invalid_version in (None, 99):
            with psycopg.connect(self.url, row_factory=dict_row) as conn:
                conn.execute(
                    "UPDATE studio_documents SET active_version=%s WHERE tenant_id='tenant-a'",
                    (invalid_version,),
                )
            with self.assertRaises(MigrationApplyError):
                with psycopg.connect(self.url, row_factory=dict_row) as conn:
                    migrate_postgres(conn)
            with psycopg.connect(self.url, row_factory=dict_row) as conn:
                self.assertEqual(
                    26,
                    conn.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"],
                )
                self.assertNotIn(
                    "workflow_version", conn.execute("SELECT * FROM studio_bindings").fetchone()
                )
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            conn.execute("UPDATE studio_documents SET active_version=2 WHERE tenant_id='tenant-a'")
            migrate_postgres(conn)
            conn.execute("UPDATE studio_documents SET active_version=1 WHERE tenant_id='tenant-a'")
        store = PostgresTaskStore(self.url, pool_min=0, pool_max=0, auto_migrate=False)
        self.addCleanup(store.close)
        pinned = store.get_studio_binding("tenant-a", "demo/repo")
        self.assertEqual((2, 1), (pinned["version"], pinned["revision"]))
        for key, version, revision, error in (
            ("flow", 3, 1, psycopg.errors.ForeignKeyViolation),
            ("flow", 99, 1, psycopg.errors.ForeignKeyViolation),
            ("flow", None, 1, psycopg.errors.CheckViolation),
            (None, 2, 1, psycopg.errors.CheckViolation),
            ("flow", 0, 1, psycopg.errors.CheckViolation),
            ("flow", 2, 0, psycopg.errors.CheckViolation),
        ):
            with self.assertRaises(error), store._connect() as conn:
                conn.execute(
                    "UPDATE studio_bindings SET workflow_id=%s,workflow_version=%s,revision=%s",
                    (key, version, revision),
                )
        self.assertEqual(pinned, store.get_studio_binding("tenant-a", "demo/repo"))

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
