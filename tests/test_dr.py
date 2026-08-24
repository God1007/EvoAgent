import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from evoagent.dr import (
    DatabaseFingerprint,
    DisasterRecoveryError,
    TableFingerprint,
    _compare_fingerprints,
    _objective_report,
    _postgres_parts,
    _run_pg_dump_bounded,
    _run_pg_tool,
    run_postgres_drill,
)


class RecoverySafetyTests(unittest.TestCase):
    def test_fingerprint_comparison_fails_on_any_difference(self):
        original = DatabaseFingerprint("schema", 1, {"tasks": TableFingerprint(1, "a" * 64)})
        changed = DatabaseFingerprint("schema", 1, {"tasks": TableFingerprint(2, "b" * 64)})

        with self.assertRaisesRegex(DisasterRecoveryError, "differs"):
            _compare_fingerprints(original, changed)

    def test_objectives_distinguish_rpo_and_rto_failures(self):
        objectives = _objective_report(61, 9, 60, 10)

        self.assertFalse(objectives["rpo"]["met"])
        self.assertTrue(objectives["rto"]["met"])

    def test_postgres_command_conninfo_never_contains_password(self):
        with patch.dict(
            os.environ,
            {
                "EVOAGENT_DATABASE_URL": "postgresql://leaked:secret@example/db",
                "EVOAGENT_AUTH_SECRET": "jwt-secret",
                "GITHUB_TOKEN": "github-secret",
                "MODEL_API_KEY": "model-secret",
                "PGPASSFILE": "/secure/pgpass",
            },
        ):
            parts, command_conninfo, environment = _postgres_parts(
                "postgresql://dr-user:p%40ss@db.example:5432/evoagent?sslmode=require"
            )

        self.assertEqual("p@ss", parts["password"])
        self.assertNotIn("p@ss", command_conninfo)
        self.assertEqual("p@ss", environment["PGPASSWORD"])
        self.assertNotIn("EVOAGENT_DATABASE_URL", environment)
        self.assertNotIn("EVOAGENT_AUTH_SECRET", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("MODEL_API_KEY", environment)
        self.assertEqual("/secure/pgpass", environment["PGPASSFILE"])

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

    def test_validation_and_cleanup_failures_both_remain_actionable(self):
        connection = unittest.mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = {"snapshot_id": "snapshot"}
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = connection
        fingerprint = DatabaseFingerprint("schema", 1, {"tasks": TableFingerprint(1, "a" * 64)})

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("psycopg.connect", return_value=context) as connect,
            patch("evoagent.dr._postgres_fingerprint", return_value=fingerprint),
            patch("evoagent.dr._run_pg_dump_bounded"),
            patch("evoagent.dr.os.path.getsize", return_value=4),
            patch("evoagent.dr._sha256_file", return_value="a" * 64),
            patch("evoagent.dr._create_drill_database"),
            patch("evoagent.dr._run_pg_tool", side_effect=RuntimeError("restore failed")),
            patch("evoagent.dr._drop_drill_database", side_effect=RuntimeError("cleanup failed")),
            self.assertRaisesRegex(
                DisasterRecoveryError,
                "validation failed: restore failed; restored database cleanup failed: "
                "cleanup failed; manual cleanup required for evoagent_drill_",
            ),
        ):
            run_postgres_drill("postgresql://user:password@localhost/source", directory)

        for call in connect.call_args_list:
            self.assertEqual(30, call.kwargs["connect_timeout"])
            self.assertEqual("-c statement_timeout=900000", call.kwargs["options"])


if __name__ == "__main__":
    unittest.main()
