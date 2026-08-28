import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from evoagent.migrate import run
from evoagent.migrations import CURRENT_SCHEMA_VERSION, SchemaTooNewError


class MigrationCliTests(unittest.TestCase):
    def test_help_and_unknown_arguments_never_open_the_database(self):
        for arguments, code in ((["--help"], 0), (["--dry-run"], 2), (["unexpected"], 2)):
            with self.subTest(arguments=arguments):
                with (
                    patch("sys.argv", ["evoagent-migrate", *arguments]),
                    patch(
                        "evoagent.migrate.Settings.from_env",
                        side_effect=AssertionError("read configuration"),
                    ) as settings,
                    patch("evoagent.migrate.create_store") as create,
                    redirect_stdout(StringIO()),
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit) as result,
                ):
                    run()
                self.assertEqual(code, result.exception.code)
                settings.assert_not_called()
                create.assert_not_called()

    def test_prints_machine_readable_postgres_status(self):
        store = unittest.mock.Mock()
        store.schema_version.return_value = CURRENT_SCHEMA_VERSION
        output = StringIO()
        with (
            patch("evoagent.migrate.create_store", return_value=store) as create,
            redirect_stdout(output),
        ):
            run([])
        payload = json.loads(output.getvalue())
        self.assertEqual("migrated", payload["status"])
        self.assertEqual("postgresql", payload["backend"])
        self.assertEqual(CURRENT_SCHEMA_VERSION, payload["schema_version"])
        self.assertIs(create.call_args.kwargs["auto_migrate"], True)
        store.close.assert_called_once()

    def test_schema_compatibility_failure_has_nonzero_machine_readable_result(self):
        error = SchemaTooNewError("database is newer")
        stderr = StringIO()
        with (
            patch("evoagent.migrate.create_store", side_effect=error),
            redirect_stderr(stderr),
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            run([])
        payload = json.loads(stderr.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertIn("newer", payload["error"])

    def test_unexpected_failure_is_machine_readable_and_redacted(self):
        stderr = StringIO()
        with (
            patch(
                "evoagent.migrate.create_store",
                side_effect=RuntimeError("password=deployment-secret"),
            ),
            redirect_stderr(stderr),
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            run([])

        payload = json.loads(stderr.getvalue())
        self.assertEqual("error", payload["status"])
        self.assertEqual("schema migration failed (RuntimeError)", payload["error"])
        self.assertNotIn("deployment-secret", stderr.getvalue())

    def test_configuration_failure_uses_the_same_json_boundary(self):
        stderr = StringIO()
        with (
            patch(
                "evoagent.migrate.Settings.from_env",
                side_effect=ValueError("EVOAGENT_PG_POOL_MAX must be positive"),
            ),
            redirect_stderr(stderr),
            self.assertRaisesRegex(SystemExit, "2"),
        ):
            run([])

        self.assertEqual(
            {
                "status": "error",
                "error": "EVOAGENT_PG_POOL_MAX must be positive",
            },
            json.loads(stderr.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
