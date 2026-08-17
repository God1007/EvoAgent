import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from evoagent.dr import (
    DisasterRecoveryError,
    _compare_fingerprints,
    _objective_report,
    _postgres_parts,
    _run_pg_dump_bounded,
    _run_pg_tool,
    _sqlite_fingerprint,
    main,
    run_sqlite_drill,
)
from evoagent.migrations import CURRENT_SCHEMA_VERSION
from evoagent.store import TaskStore


class SQLiteRecoveryDrillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = os.path.join(self.directory.name, "source.db")
        self.output = os.path.join(self.directory.name, "recovery")
        store = TaskStore(self.source)
        store.create("task-1", "acme/widgets", 7, {"source": "dr-test"}, "tenant-a")
        store.save_task_payload("task-1", "diff --git a/a.py b/a.py\n")
        store.audit("tenant-a", "tester", "fixture.created", "task-1", {"ok": True})

    def test_online_backup_is_restored_validated_and_cleaned(self):
        source_before = _sqlite_fingerprint(self.source)

        report = run_sqlite_drill(self.source, self.output, 60, 60)

        self.assertEqual("pass", report["status"])
        self.assertEqual("sqlite", report["backend"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, report["integrity"]["schema_version"])
        self.assertEqual(
            report["integrity"]["source_fingerprint"],
            report["integrity"]["restored_fingerprint"],
        )
        self.assertEqual(source_before, _sqlite_fingerprint(self.source))
        self.assertTrue(report["cleanup"]["restored_copy_removed"])
        self.assertTrue(os.path.isfile(report["artifact"]["path"]))
        self.assertTrue(os.path.isfile(report["manifest_path"]))
        self.assertEqual(
            stat.S_IRUSR | stat.S_IWUSR,
            stat.S_IMODE(os.stat(report["artifact"]["path"]).st_mode),
        )
        with open(report["manifest_path"], encoding="utf-8") as handle:
            self.assertEqual(report, json.load(handle))

    def test_schema_history_tampering_fails_closed(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")

        with self.assertRaisesRegex(DisasterRecoveryError, "checksum"):
            _sqlite_fingerprint(self.source)

    def test_cli_emits_machine_readable_pass_report(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = main(
                [
                    "--backend",
                    "sqlite",
                    "--sqlite-path",
                    self.source,
                    "--output-dir",
                    self.output,
                    "--max-rpo-seconds",
                    "60",
                    "--max-rto-seconds",
                    "60",
                ]
            )

        self.assertEqual(0, status)
        self.assertEqual("pass", json.loads(output.getvalue())["status"])

    def test_cli_reports_error_without_traceback(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            status = main(
                [
                    "--backend",
                    "sqlite",
                    "--sqlite-path",
                    os.path.join(self.directory.name, "missing.db"),
                    "--output-dir",
                    self.output,
                ]
            )

        self.assertEqual(2, status)
        self.assertEqual("error", json.loads(error.getvalue())["status"])


class RecoverySafetyTests(unittest.TestCase):
    def test_fingerprint_comparison_fails_on_any_difference(self):
        original = _sqlite_fingerprint(self._database())
        changed_path = self._database()
        with sqlite3.connect(changed_path) as connection:
            connection.execute("INSERT INTO tasks SELECT * FROM tasks WHERE 0")
            connection.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES ('x','x','x','x','{}','2026-01-01T00:00:00+00:00')"
            )
        changed = _sqlite_fingerprint(changed_path)

        with self.assertRaisesRegex(DisasterRecoveryError, "differs"):
            _compare_fingerprints(original, changed)

    def _database(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "fixture.db")
        TaskStore(path)
        return path

    def test_objectives_distinguish_rpo_and_rto_failures(self):
        objectives = _objective_report(61, 9, 60, 10)

        self.assertFalse(objectives["rpo"]["met"])
        self.assertTrue(objectives["rto"]["met"])

    def test_postgres_command_conninfo_never_contains_password(self):
        with patch.dict(
            os.environ,
            {"EVOAGENT_DATABASE_URL": "postgresql://leaked:secret@example/db"},
        ):
            parts, command_conninfo, environment = _postgres_parts(
                "postgresql://dr-user:p%40ss@db.example:5432/evoagent?sslmode=require"
            )

        self.assertEqual("p@ss", parts["password"])
        self.assertNotIn("p@ss", command_conninfo)
        self.assertEqual("p@ss", environment["PGPASSWORD"])
        self.assertNotIn("EVOAGENT_DATABASE_URL", environment)

    def test_pg_tool_uses_argument_vector_and_propagates_failure(self):
        _run_pg_tool(sys.executable, ["-c", "raise SystemExit(0)"], dict(os.environ), 5)
        with self.assertRaisesRegex(DisasterRecoveryError, "command failed"):
            _run_pg_tool(sys.executable, ["-c", "raise SystemExit(3)"], dict(os.environ), 5)

    def test_pg_dump_stream_is_bounded_and_partial_artifact_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, "bounded.dump")
            _run_pg_dump_bounded(
                sys.executable,
                ["-c", "import sys; sys.stdout.buffer.write(b'x' * 10)"],
                artifact,
                dict(os.environ),
                5,
                100,
            )
            self.assertEqual(10, os.path.getsize(artifact))

            oversized = os.path.join(directory, "oversized.dump")
            with self.assertRaisesRegex(DisasterRecoveryError, "byte limit"):
                _run_pg_dump_bounded(
                    sys.executable,
                    ["-c", "import sys; sys.stdout.buffer.write(b'x' * 1000)"],
                    oversized,
                    dict(os.environ),
                    5,
                    100,
                )
            self.assertFalse(os.path.exists(oversized))


if __name__ == "__main__":
    unittest.main()
