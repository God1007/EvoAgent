import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import evoagent.migrations as migration_module
from evoagent.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    MigrationApplyError,
    SchemaHistoryError,
    SchemaTooNewError,
    migrate_sqlite,
    validate_current_schema_history,
)
from evoagent.models import TaskState
from evoagent.store import TaskStore, utc_now


class SQLiteMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "evoagent.db")

    def tearDown(self):
        self.directory.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def test_fresh_store_records_contiguous_checksummed_history(self):
        store = TaskStore(self.path)
        self.assertEqual(CURRENT_SCHEMA_VERSION, store.schema_version())

        with self.connect() as conn:
            rows = conn.execute(
                "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
                ).fetchall()
            }
        self.assertEqual([item.version for item in MIGRATIONS], [row["version"] for row in rows])
        self.assertEqual([item.name for item in MIGRATIONS], [row["name"] for row in rows])
        self.assertEqual([item.checksum for item in MIGRATIONS], [row["checksum"] for row in rows])
        self.assertTrue(all(row["applied_at"] for row in rows))
        self.assertEqual(CURRENT_SCHEMA_VERSION, validate_current_schema_history(list(rows)))
        self.assertIn("idx_tasks_recovery", indexes)
        self.assertIn("idx_audit_recovery_epoch", indexes)
        self.assertIn("idx_model_usage_reconciliation", indexes)

    def test_read_only_operational_gate_refuses_an_old_schema(self):
        with self.connect() as conn:
            migrate_sqlite(conn, CURRENT_SCHEMA_VERSION - 1)
            rows = conn.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()

        with self.assertRaisesRegex(SchemaHistoryError, "required version"):
            validate_current_schema_history(list(rows))

    def test_forward_migrates_from_previous_version_without_data_loss(self):
        previous = CURRENT_SCHEMA_VERSION - 1
        task_id = "task-" + uuid.uuid4().hex
        with self.connect() as conn:
            migrate_sqlite(conn, previous)
            now = utc_now()
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,error,"
                "created_at,updated_at,tenant_id,cancel_requested) VALUES (?,?,?,?,?,NULL,NULL,?,?,?,0)",
                (
                    task_id,
                    TaskState.PENDING.value,
                    "acme/widgets",
                    9,
                    json.dumps({"source": "previous-release"}),
                    now,
                    now,
                    "tenant-a",
                ),
            )

        store = TaskStore(self.path)
        self.assertEqual(CURRENT_SCHEMA_VERSION, store.schema_version())
        self.assertEqual("previous-release", store.get(task_id, "tenant-a")["input"]["source"])

    def test_forward_migration_removes_legacy_operational_exception_text(self):
        task_id = "legacy-error-" + uuid.uuid4().hex
        secret = "password=legacy-operational-secret"
        now = utc_now()
        with self.connect() as conn:
            migrate_sqlite(conn, CURRENT_SCHEMA_VERSION - 1)
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,error,"
                "created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (?,?,?,?,?,NULL,?,?,?,?,0)",
                (task_id, TaskState.FAILED.value, "acme/widgets", 1, "{}", secret, now, now, "t"),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (?,?,?,?,?)",
                (task_id, 1, TaskState.FAILED.value, secret, now),
            )
            conn.execute(
                "INSERT INTO failure_cases(task_id,category,payload_json,resolved,created_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, "execution_error", json.dumps({"error": secret}), 0, now),
            )
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,error,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (task_id, "reviewing", "failed", 1, "{}", secret, now),
            )
            conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,correlation_id,"
                "content_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    "agent",
                    "planner",
                    "agent_failure",
                    "",
                    json.dumps({"error": secret}),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,attempts,"
                "available_at,last_error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("outbox", "review", "outbox", "{}", "dead", 1, now, secret, now, now),
            )
            conn.execute(
                "INSERT INTO effect_receipts(effect_key,status,attempts,last_error,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?)",
                ("effect", "pending", 1, secret, now, now),
            )
            conn.execute(
                "INSERT INTO alerts(tenant_id,alert_key,severity,message,status,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                ("t", "dlq:" + task_id, "critical", secret, "open", now, now),
            )
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("t", "system", "shadow.failed", task_id, json.dumps({"error": secret}), now),
            )
            conn.commit()
            migrate_sqlite(conn)
            values = {
                "task": conn.execute("SELECT error FROM tasks WHERE id=?", (task_id,)).fetchone()[
                    0
                ],
                "trace": conn.execute(
                    "SELECT message FROM trace_events WHERE task_id=?", (task_id,)
                ).fetchone()[0],
                "failure": conn.execute(
                    "SELECT payload_json FROM failure_cases WHERE task_id=?", (task_id,)
                ).fetchone()[0],
                "checkpoint": conn.execute(
                    "SELECT error FROM checkpoints WHERE task_id=?", (task_id,)
                ).fetchone()[0],
                "agent": conn.execute(
                    "SELECT content_json FROM agent_messages WHERE task_id=?", (task_id,)
                ).fetchone()[0],
                "outbox": conn.execute(
                    "SELECT last_error FROM outbox_messages WHERE id='outbox'"
                ).fetchone()[0],
                "effect": conn.execute(
                    "SELECT last_error FROM effect_receipts WHERE effect_key='effect'"
                ).fetchone()[0],
                "alert": conn.execute(
                    "SELECT message FROM alerts WHERE alert_key=?", ("dlq:" + task_id,)
                ).fetchone()[0],
                "audit": conn.execute(
                    "SELECT detail_json FROM audit_log WHERE action='shadow.failed'"
                ).fetchone()[0],
            }

        self.assertNotIn(secret, json.dumps(values))
        self.assertEqual("review execution failed", values["task"].split(" [", 1)[0])
        self.assertEqual(
            "review agent failed", json.loads(values["agent"])["error"].split(" [", 1)[0]
        )
        self.assertEqual(
            "shadow review failed", json.loads(values["audit"])["error"].split(" [", 1)[0]
        )

    def test_adopts_unversioned_legacy_schema_and_preserves_rows(self):
        task_id = "legacy-" + uuid.uuid4().hex
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,
                    pull_request INTEGER, input_json TEXT NOT NULL, report_json TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            conn.execute(
                """CREATE TABLE installations (
                    installation_id INTEGER PRIMARY KEY, account_login TEXT NOT NULL,
                    created_at TEXT NOT NULL)"""
            )
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,NULL,NULL,?,?)",
                (
                    task_id,
                    TaskState.PENDING.value,
                    "legacy/repository",
                    3,
                    json.dumps({"legacy": True}),
                    now,
                    now,
                ),
            )

        store = TaskStore(self.path)
        task = store.get(task_id, "default")
        self.assertIsNotNone(task)
        self.assertTrue(task["input"]["legacy"])
        with self.connect() as conn:
            task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            installation_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(installations)")
            }
        self.assertIn("tenant_id", task_columns)
        self.assertIn("cancel_requested", task_columns)
        self.assertIn("tenant_id", installation_columns)

    def test_refuses_schema_created_by_newer_application(self):
        TaskStore(self.path)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (CURRENT_SCHEMA_VERSION + 1, "future", "future", utc_now()),
            )
        with self.assertRaisesRegex(SchemaTooNewError, "newer than supported"):
            TaskStore(self.path)

    def test_refuses_modified_migration_history(self):
        TaskStore(self.path)
        with self.connect() as conn:
            conn.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
        with self.assertRaisesRegex(SchemaHistoryError, "immutable application history"):
            TaskStore(self.path)

    def test_refuses_non_contiguous_history(self):
        with self.connect() as conn:
            conn.execute(
                """CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL)"""
            )
            migration = MIGRATIONS[1]
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                (migration.version, migration.name, migration.checksum, utc_now()),
            )
        with self.assertRaisesRegex(SchemaHistoryError, "not contiguous"):
            TaskStore(self.path)

    def test_failed_migration_rolls_back_schema_and_history(self):
        TaskStore(self.path)
        failing = Migration(
            CURRENT_SCHEMA_VERSION + 1,
            "failure-injection",
            ("CREATE TABLE must_rollback(id INTEGER)", "THIS IS NOT VALID SQL"),
            (),
        )
        catalog = MIGRATIONS + (failing,)
        by_version = {item.version: item for item in catalog}
        with (
            patch.object(migration_module, "MIGRATIONS", catalog),
            patch.object(migration_module, "CURRENT_SCHEMA_VERSION", CURRENT_SCHEMA_VERSION + 1),
            patch.object(migration_module, "_MIGRATION_BY_VERSION", by_version),
            self.connect() as conn,
        ):
            with self.assertRaisesRegex(MigrationApplyError, "migration failed"):
                migrate_sqlite(conn, CURRENT_SCHEMA_VERSION + 1)

        with self.connect() as conn:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='must_rollback'"
            ).fetchone()
            newest = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
        self.assertIsNone(table)
        self.assertEqual(CURRENT_SCHEMA_VERSION, newest["version"])

    def test_concurrent_startup_serializes_migration(self):
        barrier = threading.Barrier(3)
        versions: list[int] = []
        failures: list[Exception] = []

        def start():
            barrier.wait()
            try:
                versions.append(TaskStore(self.path).schema_version())
            except Exception as exc:  # pragma: no cover - assertion captures diagnostics
                failures.append(exc)

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(10)

        self.assertFalse(failures)
        self.assertEqual([CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION], sorted(versions))

    def test_target_version_is_bounded(self):
        with self.connect() as conn:
            with self.assertRaisesRegex(ValueError, "target schema version"):
                migrate_sqlite(conn, CURRENT_SCHEMA_VERSION + 1)


if __name__ == "__main__":
    unittest.main()
