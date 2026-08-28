import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime
from unittest.mock import patch

from evoagent.agents import MultiAgentCoordinator
from evoagent.dr import (
    DatabaseFingerprint,
    DisasterRecoveryError,
    TableFingerprint,
    _canonical,
    _compare_fingerprints,
    _create_drill_database,
    _drop_drill_database,
    _objective_report,
    _postgres_parts,
    _postgres_target_url,
    _run_pg_dump_bounded,
    _run_pg_tool,
    main,
    run_postgres_drill,
)
from evoagent.harness import ReviewHarness
from evoagent.postgres_store import PostgresTaskStore
from evoagent.reviewer import LocalRuleReviewer
from evoagent.workflow import AgentSpec, PayloadType, Step, _json
from examples.custom_review_workflow import build_workflow, business_policy
from tests.db_support import postgres_url


class RecoverySafetyTests(unittest.TestCase):
    def test_fingerprint_comparison_fails_on_any_difference(self):
        original = DatabaseFingerprint("schema", 1, {"tasks": TableFingerprint(1, "a" * 64)})
        changed = DatabaseFingerprint("schema", 1, {"tasks": TableFingerprint(2, "b" * 64)})

        with self.assertRaisesRegex(DisasterRecoveryError, "differs"):
            _compare_fingerprints(original, changed)

        self.assertEqual(
            _canonical(datetime.fromisoformat("2026-08-28T00:00:00+00:00")),
            _canonical(datetime.fromisoformat("2026-08-28T08:00:00+08:00")),
        )
        for value in (date(2026, 8, 28), datetime(2026, 8, 28, 8)):
            self.assertEqual(value.isoformat(), _canonical(value))

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


class PostgresWorkflowRecoveryTests(unittest.TestCase):
    def test_real_backup_preserves_handoffs_across_timezones_and_resumes(self):
        url = postgres_url(self)
        if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
            self.skipTest("PostgreSQL client tools are not configured")
        import psycopg
        from psycopg import sql

        parts, _, _ = _postgres_parts(url)
        admin = self.enterContext(
            psycopg.connect(_postgres_target_url(parts, "postgres"), autocommit=True)
        )

        def database():
            name = "evoagent_drill_" + uuid.uuid4().hex
            _create_drill_database(admin, name)
            self.addCleanup(_drop_drill_database, admin, name)
            return name, _postgres_target_url(parts, name)

        source_name, source_url = database()
        admin.execute(
            sql.SQL("ALTER DATABASE {} SET timezone TO 'Asia/Shanghai'").format(
                sql.Identifier(source_name)
            )
        )
        source = PostgresTaskStore(source_url, pool_min=0, pool_max=0)
        self.addCleanup(source.close)
        task_id = "restored-agent-handoff"
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+eval(user_input)\n"
        source.create(task_id, "dr/fixture", 7, {})
        handoffs = []
        numbers = {"negative_zero": -0.0, "large_float": 1e20, "large_integer": 10**100}
        numeric_type = PayloadType("numeric-json", 1, lambda value: None)
        producer = unittest.mock.Mock(return_value={"value": numbers})
        numeric_agent = AgentSpec("numbers", "b" * 64, {}, {"value": numeric_type}, producer)

        def receiver(handoff):
            self.assertEqual(_json(numbers), _json(handoff.inputs["numbers"]))
            handoffs.append(handoff)
            if len(handoffs) == 1:
                raise RuntimeError("receiver interrupted")
            return business_policy(handoff)

        def factory(catalog):
            flow = build_workflow(catalog)
            last = flow.steps[-1]
            return replace(
                flow,
                steps=(
                    *flow.steps[:-1],
                    Step("numbers", numeric_agent, {}),
                    replace(
                        last,
                        agent=replace(
                            last.agent,
                            inputs={**last.agent.inputs, "numbers": numeric_type},
                            run=receiver,
                        ),
                        sources={**last.sources, "numbers": "numbers.value"},
                    ),
                ),
            )

        def harness(store, reviewer):
            return ReviewHarness(
                store,
                MultiAgentCoordinator(
                    [reviewer], store=store, checkpoint_revision="a" * 64, workflow_factory=factory
                ),
                node_retries=0,
            )

        with self.assertRaisesRegex(RuntimeError, "receiver interrupted"):
            harness(source, LocalRuleReviewer()).run(task_id, "dr/fixture", 7, diff)
        before = source.load_checkpoints(task_id)
        self.assertEqual("failed", before["workflow:business"]["status"])
        self.assertEqual(
            8,
            sum(
                key.startswith("workflow:") and value["status"] == "completed"
                for key, value in before.items()
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, {"EVOAGENT_DATABASE_URL": source_url}),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = main(
                    [
                        "--output-dir",
                        directory,
                        "--max-rpo-seconds",
                        "30",
                        "--max-rto-seconds",
                        "30",
                        "--command-timeout-seconds",
                        "30",
                    ]
                )
            self.assertEqual(0, code, errors.getvalue())
            report = json.loads(output.getvalue())
            self.assertEqual("pass", report["status"])
            self.assertEqual(
                report["integrity"]["source_fingerprint"],
                report["integrity"]["restored_fingerprint"],
            )
            self.assertTrue(report["cleanup"]["restored_database_removed"])

            target_name, target_url = database()
            admin.execute(
                sql.SQL("ALTER DATABASE {} SET timezone TO 'UTC'").format(
                    sql.Identifier(target_name)
                )
            )
            _, command_url, environment = _postgres_parts(target_url)
            _run_pg_tool(
                "pg_restore",
                [
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--dbname=" + command_url,
                    report["artifact"]["path"],
                ],
                environment,
                30,
            )
            target = PostgresTaskStore(target_url, pool_min=0, pool_max=0, auto_migrate=False)
            self.addCleanup(target.close)
            self.assertEqual(before, target.load_checkpoints(task_id))
            reviewer = LocalRuleReviewer()
            reviewer.review = unittest.mock.Mock(
                side_effect=AssertionError("completed Agent reran")
            )
            restored = harness(target, reviewer).run(task_id, "dr/fixture", 7, diff)
            self.assertEqual("SEC-EVAL", restored.findings[0].rule_id)
            self.assertEqual("SUCCESS", target.get(task_id)["state"])
            reviewer.review.assert_not_called()
            producer.assert_called_once()
            after = target.load_checkpoints(task_id)
            for node, checkpoint in before.items():
                if checkpoint["status"] == "completed":
                    self.assertEqual(checkpoint, after[node])
            self.assertEqual([1, 2], [handoff.attempt for handoff in handoffs])
            self.assertEqual(handoffs[0].idempotency_key, handoffs[1].idempotency_key)
            self.assertEqual(before, source.load_checkpoints(task_id))
            self.assertEqual("FAILED", source.get(task_id)["state"])


if __name__ == "__main__":
    unittest.main()
