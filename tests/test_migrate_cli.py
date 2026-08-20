import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from evoagent.migrate import run
from evoagent.migrations import CURRENT_SCHEMA_VERSION, SchemaTooNewError


class MigrationCliTests(unittest.TestCase):
    def test_prints_machine_readable_postgres_status(self):
        store = unittest.mock.Mock()
        store.schema_version.return_value = CURRENT_SCHEMA_VERSION
        output = StringIO()
        with patch("evoagent.migrate.create_store", return_value=store), redirect_stdout(output):
            run()
        payload = json.loads(output.getvalue())
        self.assertEqual("migrated", payload["status"])
        self.assertEqual("postgresql", payload["backend"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, payload["schema_version"])
        store.close.assert_called_once()

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
