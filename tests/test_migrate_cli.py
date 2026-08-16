import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from evoagent.migrate import run
from evoagent.migrations import CURRENT_SCHEMA_VERSION, SchemaTooNewError


class MigrationCliTests(unittest.TestCase):
    def test_migrates_configured_sqlite_database_and_prints_machine_readable_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "cli.db")
            output = StringIO()
            with (
                patch.dict(
                    os.environ,
                    {"EVOAGENT_DB_PATH": path, "EVOAGENT_DATABASE_URL": ""},
                    clear=False,
                ),
                redirect_stdout(output),
            ):
                run()
        payload = json.loads(output.getvalue())
        self.assertEqual("migrated", payload["status"])
        self.assertEqual("sqlite", payload["backend"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, payload["schema_version"])

    def test_schema_compatibility_failure_has_nonzero_machine_readable_result(self):
        error = SchemaTooNewError("database is newer")
        stderr = StringIO()
        with (
            patch("evoagent.migrate.create_store", side_effect=error),
            redirect_stderr(stderr),
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            run()
        payload = json.loads(stderr.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertIn("newer", payload["error"])


if __name__ == "__main__":
    unittest.main()
