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
