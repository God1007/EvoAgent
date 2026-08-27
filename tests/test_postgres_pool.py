import json
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest import mock

from evoagent.metrics import Metrics
from evoagent.models import ReviewReport, TaskState, TraceEvent
from evoagent.postgres_store import PostgresTaskStore, create_store
from evoagent.time_utils import utc_now


class FakeConn:
    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql, *args):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return {"database_name": "evoagent"}


class PoolExhausted(RuntimeError):
    pass


class FakePool:
    def __init__(self):
        self.checked_out = 0
        self.max_concurrent = 0
        self.closed = False
        self.exhaust = False

    @contextmanager
    def connection(self, timeout=None):
        if self.exhaust:
            raise PoolExhausted("pool timeout")
        self.checked_out += 1
        self.max_concurrent = max(self.max_concurrent, self.checked_out)
        try:
            yield FakeConn()
        finally:
            self.checked_out -= 1  # returned to pool, not closed

    def get_stats(self):
        return {"pool_size": 5, "pool_available": 3, "requests_waiting": 1}

    def close(self):
        self.closed = True


class FakePsycopg:
    def __init__(self):
        self.connect_calls = 0
        self.sentinel = object()
        self.connect_timeout = None
        self.options = None

    def connect(self, url, row_factory=None, connect_timeout=None, options=None):
        self.connect_calls += 1
        self.connect_timeout = connect_timeout
        self.options = options
        return self.sentinel


def _store_with_pool(pool):
    store = object.__new__(PostgresTaskStore)
    store.psycopg = FakePsycopg()
    store.dict_row = object()
    store.url = "postgresql://example/db"
    store.pool_timeout = 5.0
    store._connect_timeout = 5
    store.statement_timeout_seconds = 120.0
    store._connection_options = "-c statement_timeout=120000 -c timezone=UTC"
    store._pool = pool
    return store


class AlertPersistenceTests(unittest.TestCase):
    def test_repeated_open_alert_refreshes_operational_state(self):
        store = object.__new__(PostgresTaskStore)
        connection = mock.MagicMock()

        @contextmanager
        def connect():
            yield connection

        with mock.patch.object(store, "_connect", connect):
            store.create_alert("tenant", "failure-rate", "critical", "still failing")

        sql, parameters = connection.execute.call_args.args
        self.assertIn("DO UPDATE SET", sql)
        self.assertIn("updated_at=EXCLUDED.updated_at", sql)
        self.assertEqual(parameters[-2], parameters[-1])


class WorkflowReadTests(unittest.TestCase):
    def store(self, rows):
        connection = mock.Mock()
        connection.execute.return_value.fetchall.return_value = rows
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)
        return store, connection

    def test_checkpoint_reads_can_filter_by_exact_node(self):
        now = datetime.now(UTC)
        row = {
            "node": "workflow:first",
            "status": "completed",
            "attempt": 1,
            "state_json": {"outputs": {"value": 2}},
            "error": None,
            "updated_at": now,
        }
        for node in (None, "workflow:first"):
            with self.subTest(node=node):
                store, connection = self.store([row])
                result = store.load_checkpoints("task", node)
                sql, parameters = connection.execute.call_args.args
                self.assertEqual(node is not None, "AND node=%s" in sql)
                self.assertEqual(("task", node) if node else ("task",), parameters)
                self.assertEqual(row["state_json"], result[row["node"]]["state"])
                self.assertEqual(now.isoformat(), result[row["node"]]["updated_at"])

    def test_workflow_status_projects_metadata_with_a_tenant_predicate(self):
        now = datetime.now(UTC)
        steps = [
            {
                "id": name,
                "agent": "agent",
                "revision": "a" * 64,
                "inputs": {"value": "number@1"},
                "outputs": {"value": "number@1"},
                "sources": {"value": source},
                "credentials": "private-step-data",
            }
            for name, source in (("first", "$input.value"), ("second", "first.value"))
        ]
        manifest = {
            "task_id": "task",
            "task_state": "EXECUTING",
            "artifacts_pruned_at": None,
            "node": "workflow",
            "definition": {
                "name": "numbers",
                "protocol_version": 1,
                "steps": steps,
                "credentials": "private-manifest-data",
            },
            "workflow_revision": "b" * 64,
            "execution_revision": "c" * 64,
        }
        for status in ("running", "failed", "completed"):
            with self.subTest(status=status):
                store, connection = self.store(
                    [
                        manifest,
                        {
                            "node": "workflow:first",
                            "status": status,
                            "attempt": 2,
                            "generation": 3,
                            "idempotency_key": "d" * 64,
                            "input_sha256": "e" * 64,
                            "output_sha256": "f" * 64 if status == "completed" else None,
                            "updated_at": now,
                            "error": "private-error-text" if status == "failed" else None,
                            "state_json": {"outputs": {"secret": "private-code-body"}},
                        },
                    ]
                )
                snapshot = store.workflow_status("task", "tenant-a")
                sql, parameters = connection.execute.call_args.args
                self.assertEqual(("workflow:%", "task", "tenant-a"), parameters)
                self.assertIn("checkpoint.node LIKE %s", sql)
                self.assertIn("task.id=%s AND task.tenant_id=%s", sql)
                self.assertIn("LEFT JOIN checkpoints", sql)
                for raw_projection in ("checkpoint.state_json,", "->'inputs'", "->'outputs'"):
                    self.assertNotIn(raw_projection, sql)
                self.assertEqual("recorded", snapshot["availability"])
                self.assertEqual("b" * 64, snapshot["workflow"]["revision"])
                first, second = snapshot["steps"]
                self.assertEqual(status, first["status"])
                self.assertEqual(2, first["attempt"])
                self.assertEqual(3, first["generation"])
                self.assertEqual(now.isoformat(), first["updated_at"])
                self.assertEqual("pending", second["status"])
                self.assertEqual([] if status == "completed" else ["first"], second["blocked_by"])
                self.assertNotIn("private-", json.dumps(snapshot))

    def test_workflow_status_distinguishes_missing_unrecorded_and_pruned(self):
        store, _ = self.store([])
        self.assertIsNone(store.workflow_status("missing", "tenant-a"))
        for pruned in (None, "2030-01-01T00:00:00+00:00"):
            store, _ = self.store(
                [
                    {
                        "task_id": "task",
                        "task_state": "SUCCESS",
                        "artifacts_pruned_at": pruned,
                        "node": None,
                    }
                ]
            )
            snapshot = store.workflow_status("task", "tenant-a")
            self.assertEqual("pruned" if pruned else "not_recorded", snapshot["availability"])
            self.assertEqual(pruned, snapshot["artifacts_pruned_at"])
            self.assertIsNone(snapshot["workflow"])
            self.assertEqual([], snapshot["steps"])


class PoolCheckoutTests(unittest.TestCase):
    def test_connect_checks_out_and_returns_to_pool(self):
        pool = FakePool()
        store = _store_with_pool(pool)
        with store._connect() as conn:
            self.assertIsInstance(conn, FakeConn)
            self.assertEqual(1, pool.checked_out)
        self.assertEqual(0, pool.checked_out)  # returned, not leaked/closed

    def test_ping_uses_pool(self):
        pool = FakePool()
        store = _store_with_pool(pool)
        store.ping()
        self.assertEqual(0, pool.checked_out)

    def test_database_confirmation_uses_server_reported_identity(self):
        store = _store_with_pool(FakePool())
        self.assertEqual("evoagent", store.connected_database_name())

    def test_exhaustion_timeout_propagates(self):
        pool = FakePool()
        pool.exhaust = True
        store = _store_with_pool(pool)
        with self.assertRaises(PoolExhausted):
            with store._connect():
                pass

    def test_pool_stats_exposed(self):
        store = _store_with_pool(FakePool())
        stats = store.pool_stats()
        self.assertEqual(5, stats["pool_size"])
        self.assertEqual(3, stats["pool_available"])

    def test_close_closes_pool(self):
        pool = FakePool()
        store = _store_with_pool(pool)
        store.close()
        self.assertTrue(pool.closed)


class CredentialMutationTests(unittest.TestCase):
    def test_password_change_and_audit_use_the_same_transaction(self):
        connection = mock.Mock()
        updated = mock.Mock()
        updated.fetchone.return_value = {"id": "user-1"}
        connection.execute.side_effect = [updated, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(
            store.change_user_password(
                "user-1", "old-password-hash", "new-password-hash", "alice", "tenant-a"
            )
        )

        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("UPDATE users", connection.execute.call_args_list[0].args[0])
        self.assertIn("INSERT INTO audit_log", connection.execute.call_args_list[1].args[0])
        self.assertNotIn("password-hash", str(connection.execute.call_args_list[1]))


class ReleaseMutationTests(unittest.TestCase):
    def test_deployment_save_returns_and_audits_in_the_same_transaction(self):
        connection = mock.Mock()
        deployment = {"skill_name": "llm-review", "generation": 3, "status": "running"}
        updated = mock.Mock()
        updated.fetchone.return_value = deployment
        connection.execute.side_effect = [mock.Mock(), updated, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.save_deployment(
            "tenant-a",
            "llm-review",
            {"candidate_version": 2, "status": "running"},
            "alice",
        )

        self.assertEqual(deployment, result)
        self.assertEqual(3, connection.execute.call_count)
        self.assertIn("RETURNING *", connection.execute.call_args_list[1].args[0])
        self.assertIn("INSERT INTO audit_log", connection.execute.call_args_list[2].args[0])

    def test_shadow_observation_and_audit_commit_together(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {"status": "running"}
        inserted = mock.Mock()
        inserted.fetchone.return_value = {"id": 1}
        updated = mock.Mock()
        updated.fetchone.return_value = {
            "status": "running",
            "shadow_samples": 1,
            "disagreements": 0,
            "samples": 0,
            "errors": 0,
            "auto_promote": False,
        }
        connection.execute.side_effect = [selected, inserted, updated, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.record_shadow_observation(
            "tenant-a",
            "llm-review",
            "task-1",
            "stable",
            {},
            {},
            0.0,
            2,
            3,
            audit_event=("shadow.completed", {"findings": 1}),
        )

        self.assertEqual("running", result["status"])
        self.assertEqual(4, connection.execute.call_count)
        audit = connection.execute.call_args_list[-1]
        self.assertIn("INSERT INTO audit_log", audit.args[0])
        self.assertEqual(
            {"findings": 1, "rollout_status": "running"},
            json.loads(audit.args[1][3]),
        )

    def test_duplicate_shadow_observation_does_not_increment_samples(self):
        connection = mock.Mock()
        deployment = {"skill_name": "llm-review", "shadow_samples": 1, "status": "running"}
        selected = mock.Mock()
        selected.fetchone.return_value = deployment
        duplicate = mock.Mock()
        duplicate.fetchone.return_value = None
        connection.execute.side_effect = [selected, duplicate]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.record_shadow_observation(
            "tenant-a", "llm-review", "task-1", "stable", {}, {}, 0.0, 2, 3
        )

        self.assertEqual(deployment, result)
        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("ON CONFLICT", connection.execute.call_args_list[1].args[0])
        self.assertIn(
            "task.id=%s AND task.tenant_id=%s", connection.execute.call_args_list[1].args[0]
        )
        self.assertEqual(("task-1", "tenant-a"), connection.execute.call_args_list[1].args[1][-2:])

    def test_duplicate_canary_result_does_not_increment_samples(self):
        connection = mock.Mock()
        deployment = {"skill_name": "llm-review", "samples": 1, "status": "running"}
        selected = mock.Mock()
        selected.fetchone.return_value = deployment
        duplicate = mock.Mock()
        duplicate.fetchone.return_value = None
        connection.execute.side_effect = [selected, duplicate]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.record_deployment_result("tenant-a", "llm-review", "task-1", False, 2, 3)

        self.assertEqual(deployment, result)
        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("_release_results", connection.execute.call_args_list[1].args[0])

    def test_canary_rollback_and_audit_commit_together(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {"status": "running"}
        recorded = mock.Mock()
        recorded.fetchone.return_value = {"id": "task-1"}
        updated = mock.Mock()
        updated.fetchone.return_value = {
            "status": "running",
            "candidate_version": 2,
            "generation": 3,
            "samples": 2,
            "errors": 1,
            "min_samples": 2,
            "max_error_rate": 0.25,
            "canary_percent": 100,
            "shadow_percent": 25,
        }
        connection.execute.side_effect = [selected, recorded, updated, mock.Mock(), mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.record_deployment_result("tenant-a", "llm-review", "task-1", True, 2, 3)

        self.assertEqual("rolled_back", result["status"])
        self.assertEqual(0, result["canary_percent"])
        self.assertIn("deployment.auto-rollback", connection.execute.call_args_list[4].args[0])
        detail = json.loads(connection.execute.call_args_list[4].args[1][2])
        self.assertEqual(0.5, detail["error_rate"])

    def test_shadow_promotion_and_audit_commit_together(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {"status": "running"}
        inserted = mock.Mock()
        inserted.fetchone.return_value = {"id": 1}
        updated = mock.Mock()
        updated.fetchone.return_value = {
            "status": "running",
            "stable_version": 1,
            "candidate_version": 2,
            "generation": 3,
            "samples": 0,
            "errors": 0,
            "max_error_rate": 0.1,
            "shadow_samples": 2,
            "disagreements": 0,
            "min_samples": 2,
            "max_disagreement_rate": 0.2,
            "auto_promote": True,
            "canary_percent": 0,
            "shadow_percent": 100,
        }
        connection.execute.side_effect = [selected, inserted, updated, mock.Mock(), mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.record_shadow_observation(
            "tenant-a", "llm-review", "task-1", "stable", {}, {}, 0.0, 2, 3
        )

        self.assertEqual("promoted", result["status"])
        self.assertEqual(2, result["stable_version"])
        self.assertEqual(0, result["shadow_percent"])
        self.assertIn("deployment.auto-promote", connection.execute.call_args_list[4].args[0])
        detail = json.loads(connection.execute.call_args_list[4].args[1][2])
        self.assertEqual(0.0, detail["disagreement_rate"])


class OutboxReplayMutationTests(unittest.TestCase):
    def test_replay_uses_task_tenant_and_audits_in_the_same_transaction(self):
        connection = mock.Mock()
        updated = mock.Mock()
        updated.fetchone.return_value = {"id": "review:task-1"}
        connection.execute.side_effect = [updated, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(store.requeue_outbox("review:task-1", "tenant-a", "alice"))

        self.assertEqual(2, connection.execute.call_count)
        replay_sql = connection.execute.call_args_list[0].args[0]
        self.assertIn("FROM tasks AS task", replay_sql)
        self.assertIn("task.tenant_id=%s", replay_sql)
        self.assertIn("RETURNING outbox.id", replay_sql)
        self.assertIn("INSERT INTO audit_log", connection.execute.call_args_list[1].args[0])

    def test_tenant_listing_joins_the_authoritative_task(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchall.return_value = []
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertEqual([], store.list_outbox("dead", 100, "tenant-a"))

        query, params = connection.execute.call_args.args
        self.assertIn("JOIN tasks AS task", query)
        self.assertIn("task.tenant_id=%s", query)
        self.assertEqual(("dead", "tenant-a", 100), params)


class ReviewCreationMutationTests(unittest.TestCase):
    def test_task_outbox_admission_and_actor_audit_share_one_transaction(self):
        connection = mock.Mock()

        def execute(sql, *_args):
            result = mock.Mock()
            if "COUNT(*) AS count" in sql:
                result.fetchone.return_value = {"count": 0}
            return result

        connection.execute.side_effect = execute
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(
            store.create_review_task(
                "task-1",
                "acme/widgets",
                7,
                {"source": "api"},
                "tenant-a",
                "diff",
                {"task_id": "task-1"},
                actor="alice",
            )
        )

        queries = [call.args[0] for call in connection.execute.call_args_list]
        self.assertTrue(any("INSERT INTO tasks" in query for query in queries))
        self.assertTrue(any("INSERT INTO task_admissions" in query for query in queries))
        self.assertTrue(any("INSERT INTO outbox_messages" in query for query in queries))
        audit = next(
            call for call in connection.execute.call_args_list if "audit_log" in call.args[0]
        )
        self.assertEqual("tenant-a", audit.args[1][0])
        self.assertEqual("alice", audit.args[1][1])
        self.assertIn('"async": true', audit.args[1][3])

    def test_idempotent_replay_audits_without_creating_a_second_task(self):
        connection = mock.Mock()
        existing = mock.Mock()
        existing.fetchone.return_value = {
            "tenant_id": "tenant-a",
            "input_json": {"idempotency_fingerprint": "same"},
        }
        connection.execute.side_effect = [mock.Mock(), existing, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertFalse(
            store.create_review_task(
                "task-1",
                "acme/widgets",
                7,
                {"idempotency_fingerprint": "same"},
                "tenant-a",
                "diff",
                {"task_id": "task-1"},
                actor="alice",
            )
        )

        queries = [call.args[0] for call in connection.execute.call_args_list]
        self.assertFalse(any("INSERT INTO tasks" in query for query in queries))
        self.assertIn("INSERT INTO audit_log", queries[-1])
        self.assertEqual("alice", connection.execute.call_args_list[-1].args[1][1])


class TaskCancellationMutationTests(unittest.TestCase):
    def test_cancel_uses_tenant_and_audits_in_the_same_transaction(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {
            "state": TaskState.PLANNING.value,
            "admission_active": True,
        }
        updated = mock.Mock(rowcount=1)
        connection.execute.side_effect = [selected, updated, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(store.request_cancel("task-1", "tenant-a", "alice"))

        self.assertEqual(3, connection.execute.call_count)
        self.assertIn("tenant_id=%s FOR UPDATE", connection.execute.call_args_list[0].args[0])
        self.assertIn("cancel_requested=TRUE", connection.execute.call_args_list[1].args[0])
        self.assertIn("INSERT INTO audit_log", connection.execute.call_args_list[2].args[0])
        self.assertIn('"changed": true', connection.execute.call_args_list[2].args[1][3])


class TaskResumeMutationTests(unittest.TestCase):
    def test_active_resume_attempt_is_audited_in_the_admission_transaction(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {
            "state": TaskState.FAILED.value,
            "repository": "acme/widgets",
            "active": True,
            "generation": 2,
        }
        connection.execute.side_effect = [mock.Mock(), selected, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.resume_review_task(
            "task-1", "tenant-a", 5, "outbox-1", "message-1", {"task_id": "task-1"}, "alice"
        )

        self.assertEqual({"status": "active"}, result)
        self.assertIn("INSERT INTO audit_log", connection.execute.call_args_list[-1].args[0])
        self.assertIn('"status": "active"', connection.execute.call_args_list[-1].args[1][3])

    def test_delivery_resume_and_audit_share_the_task_transaction(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {"state": TaskState.SUCCESS.value, "input_json": {}}
        connection.execute.side_effect = [selected, mock.Mock(), mock.Mock(), mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.resume_review_delivery(
            "task-1", "tenant-a", "outbox-1", "message-1", {"task_id": "task-1"}, "alice"
        )

        self.assertEqual({"status": "resumed"}, result)
        self.assertIn("UPDATE tasks", connection.execute.call_args_list[1].args[0])
        self.assertIn("INSERT INTO outbox_messages", connection.execute.call_args_list[2].args[0])
        self.assertIn("INSERT INTO audit_log", connection.execute.call_args_list[3].args[0])


class CheckpointFenceTests(unittest.TestCase):
    def test_cancelled_task_is_locked_and_rejected_before_checkpoint_write(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {
            "state": TaskState.CANCELLED.value,
            "cancel_requested": True,
        }
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertFalse(store.save_checkpoint("task", "executing", {"findings": []}))

        self.assertEqual(1, connection.execute.call_count)
        self.assertIn("FOR UPDATE", connection.execute.call_args.args[0])

    def test_checkpoint_upsert_cannot_regress_completion_or_attempt(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {
            "state": TaskState.EXECUTING.value,
            "cancel_requested": False,
        }
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(store.save_checkpoint("task", "executing", {}, "failed", 2, "failed"))

        query = connection.execute.call_args_list[1].args[0]
        self.assertIn("checkpoints.status<>'completed'", query)
        self.assertIn("EXCLUDED.attempt>=checkpoints.attempt", query)

    def test_successful_task_turns_all_late_worker_writes_into_noops(self):
        event = TraceEvent(2, TaskState.REVIEWING, "late", utc_now())
        writes = {
            "checkpoint": lambda store: store.save_checkpoint("task", "reviewing", {}),
            "transition": lambda store: store.transition("task", event),
            "succeed": lambda store: store.succeed(
                "task", ReviewReport("acme/widgets", 1, "late", "high"), event
            ),
            "fail": lambda store: store.fail("task", "late failure", event),
        }
        for name, write in writes.items():
            with self.subTest(name=name):
                connection = mock.Mock()
                connection.execute.return_value.fetchone.return_value = {
                    "state": TaskState.SUCCESS.value,
                    "cancel_requested": False,
                }
                manager = mock.MagicMock()
                manager.__enter__.return_value = connection
                store = object.__new__(PostgresTaskStore)
                store._connect = mock.Mock(return_value=manager)

                self.assertTrue(write(store))
                self.assertEqual(1, connection.execute.call_count)
                self.assertIn("FOR UPDATE", connection.execute.call_args.args[0])

    def test_execution_error_learning_signal_is_fenced_by_success(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {"state": TaskState.SUCCESS.value}
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        store.record_failure_case("task", "execution_error", {"error": "late"})

        self.assertEqual(1, connection.execute.call_count)
        self.assertIn("FOR UPDATE", connection.execute.call_args.args[0])

    def test_execution_error_learning_signal_rejects_stale_generation(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {
            "state": TaskState.FAILED.value,
            "generation": 2,
        }
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        store.record_failure_case("task", "execution_error", {"error": "stale"}, 1)

        self.assertEqual(1, connection.execute.call_count)
        self.assertIn("FOR UPDATE OF task", connection.execute.call_args.args[0])

        connection.reset_mock()
        connection.execute.return_value.fetchone.return_value = {
            "state": TaskState.FAILED.value,
            "cancel_requested": False,
        }
        self.assertTrue(
            store.succeed(
                "task",
                ReviewReport("acme/widgets", 1, "recovered", "low"),
                TraceEvent(2, TaskState.SUCCESS, "recovered", utc_now()),
            )
        )
        self.assertTrue(
            any(
                "UPDATE failure_cases SET resolved=TRUE" in call.args[0]
                for call in connection.execute.call_args_list
            )
        )

    def test_agent_message_write_is_bound_to_live_task_state(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = None
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertFalse(
            store.record_agent_message(
                "task",
                {
                    "sender": "agent",
                    "recipient": "planner",
                    "kind": "evidence",
                    "content": {},
                },
            )
        )

        query = connection.execute.call_args.args[0]
        self.assertIn("FOR UPDATE", query)
        self.assertIn("cancel_requested", query)


class FeedbackMutationTests(unittest.TestCase):
    def test_tenant_fence_sample_and_content_free_audit_share_one_transaction(self):
        connection = mock.Mock()
        selected = mock.Mock()
        selected.fetchone.return_value = {
            "state": TaskState.SUCCESS.value,
            "tenant_id": "tenant-a",
            "generation": 1,
        }
        connection.execute.side_effect = [selected, mock.Mock(), mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(
            store.record_failure_case(
                "task-1",
                "false_positive",
                {"note": "password=keep-only-in-sample"},
                tenant_id="tenant-a",
                actor="alice",
            )
        )

        query, params = connection.execute.call_args_list[0].args
        self.assertIn("task.tenant_id=%s", query)
        self.assertEqual(["task-1", "tenant-a"], params)
        self.assertIn("INSERT INTO failure_cases", connection.execute.call_args_list[1].args[0])
        audit_sql, audit_params = connection.execute.call_args_list[2].args
        self.assertIn("INSERT INTO audit_log", audit_sql)
        self.assertEqual("alice", audit_params[1])
        self.assertEqual('{"category": "false_positive"}', audit_params[3])
        self.assertNotIn("keep-only-in-sample", str(connection.execute.call_args_list[2]))

    def test_wrong_tenant_writes_neither_sample_nor_audit(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = None
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertFalse(
            store.record_failure_case("task-1", "accepted", {}, tenant_id="tenant-b", actor="bob")
        )
        self.assertEqual(1, connection.execute.call_count)


class SessionCompletionFenceTests(unittest.TestCase):
    def test_completed_turn_is_a_noop_inside_the_existing_session_lock(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {"summary_json": {"new": 1}}
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertFalse(
            store.complete_session_turn("session", "turn", "task", [], {"new": 1}, "sha")
        )

        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("pg_advisory_xact_lock", connection.execute.call_args_list[0].args[0])
        self.assertNotIn(
            "DELETE", " ".join(call.args[0] for call in connection.execute.call_args_list)
        )

    def test_findings_are_inserted_in_one_statement(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {"summary_json": None}
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)
        snapshots = [
            {"fingerprint": "a", "status": "new"},
            {"fingerprint": "b", "status": "still_open"},
            {"fingerprint": "c", "status": "moved"},
        ]

        self.assertTrue(
            store.complete_session_turn("session", "turn", "task", snapshots, {}, "sha")
        )

        inserts = [
            call
            for call in connection.execute.call_args_list
            if "INSERT INTO session_findings" in call.args[0]
        ]
        self.assertEqual(1, len(inserts))
        self.assertIn("jsonb_array_elements", inserts[0].args[0])
        self.assertIn("WITH ORDINALITY", inserts[0].args[0])
        self.assertEqual(snapshots, json.loads(inserts[0].args[1][3]))

    def test_older_turn_cannot_regress_latest_session_head(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {"summary_json": None}
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(store.complete_session_turn("session", "turn-2", "task", [], {}, "sha2"))

        query = connection.execute.call_args_list[-1].args[0]
        self.assertIn("later.sequence>(SELECT current.sequence", query)
        self.assertEqual("turn-2", connection.execute.call_args_list[-1].args[1][0])


class SessionInputMutationTests(unittest.TestCase):
    def test_resume_and_secret_free_actor_audit_share_one_transaction(self):
        connection = mock.Mock()
        updated = mock.Mock()
        updated.fetchone.return_value = {"tenant_id": "tenant-a"}
        connection.execute.side_effect = [updated, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertTrue(store.resolve_session_input("session-1", "tenant-a", "alice"))

        self.assertEqual(2, connection.execute.call_count)
        self.assertIn("RETURNING tenant_id", connection.execute.call_args_list[0].args[0])
        audit_sql, audit_params = connection.execute.call_args_list[1].args
        self.assertIn("INSERT INTO audit_log", audit_sql)
        self.assertEqual(("tenant-a", "alice", "session-1", mock.ANY), audit_params)
        self.assertNotIn("message", audit_sql)


class SessionTimelineQueryTests(unittest.TestCase):
    def test_findings_for_all_turns_use_one_query(self):
        now = mock.Mock()
        now.isoformat.return_value = "2026-08-24T00:00:00+00:00"
        session = mock.Mock()
        session.fetchone.return_value = {
            "id": "session-1",
            "tenant_id": "tenant-a",
            "created_at": now,
            "updated_at": now,
            "last_webhook_at": None,
        }
        turns = mock.Mock()
        turns.fetchall.return_value = [
            {
                "id": "turn-2",
                "summary_json": {"open": 2},
                "created_at": now,
                "findings_pruned_at": None,
            },
            {
                "id": "turn-1",
                "summary_json": {"open": 1},
                "created_at": now,
                "findings_pruned_at": None,
            },
        ]
        findings = mock.Mock()
        findings.fetchall.return_value = [
            {"turn_id": "turn-1", "snapshot_json": {"rule_id": "A"}},
            {"turn_id": "turn-2", "snapshot_json": {"rule_id": "B"}},
            {"turn_id": "turn-2", "snapshot_json": {"rule_id": "C"}},
        ]
        connection = mock.Mock()
        connection.execute.side_effect = [session, turns, findings]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        timeline = store.get_session_timeline("session-1", "tenant-a")

        self.assertEqual(3, connection.execute.call_count)
        self.assertIn("turn_id=ANY", connection.execute.call_args_list[2].args[0])
        self.assertEqual(["turn-1", "turn-2"], [turn["id"] for turn in timeline["turns"]])
        self.assertEqual(
            [[{"rule_id": "A"}], [{"rule_id": "B"}, {"rule_id": "C"}]],
            [turn["findings"] for turn in timeline["turns"]],
        )


class TenantTaskLookupTests(unittest.TestCase):
    def test_lookup_is_one_tenant_scoped_query(self):
        cursor = mock.Mock()
        cursor.fetchall.return_value = [{"id": "task-a"}]
        connection = mock.Mock()
        connection.execute.return_value = cursor
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.tenant_task_ids("tenant-a", ["task-a", "task-a", "", "task-b"])

        self.assertEqual({"task-a"}, result)
        query, params = connection.execute.call_args.args
        self.assertIn("tenant_id=%s AND id=ANY(%s)", query)
        self.assertEqual(("tenant-a", ["task-a", "task-b"]), params)


class OutboxClaimQueryTests(unittest.TestCase):
    def test_batch_claim_is_one_atomic_statement(self):
        cursor = mock.Mock()
        cursor.fetchall.return_value = [
            {
                "id": "review:task-a",
                "message_key": "task-a",
                "attempts": 2,
                "payload_json": '{"task_id":"task-a"}',
            }
        ]
        connection = mock.Mock()
        connection.execute.return_value = cursor
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        claimed = store.claim_outbox("worker-a", 500, 30, 5)

        self.assertEqual(1, connection.execute.call_count)
        query, params = connection.execute.call_args.args
        self.assertIn("WITH expired_candidates AS (SELECT id FROM outbox_messages", query)
        self.assertIn("expired AS (UPDATE outbox_messages AS message SET status='dead'", query)
        self.assertIn("lease_until<%s AND attempts>=%s", query)
        self.assertIn("FOR UPDATE SKIP LOCKED", query)
        self.assertIn("UPDATE outbox_messages", query)
        self.assertIn("RETURNING message.*", query)
        self.assertEqual(500, params[2])
        self.assertRegex(params[3], r"^outbox dispatch failed \[type=unknown; ref=[0-9a-f]{16}\]$")
        self.assertEqual(500, params[8])
        self.assertEqual("worker-a", params[9])
        self.assertEqual(2, claimed[0]["attempts"])
        self.assertEqual({"task_id": "task-a"}, claimed[0]["payload"])


class OperationalRetentionQueryTests(unittest.TestCase):
    def test_release_observations_are_bounded_but_running_rollouts_are_protected(self):
        connection = mock.Mock()

        def execute(sql, *_args):
            cursor = mock.Mock()
            cursor.fetchall.return_value = (
                [{"id": 7}] if "DELETE FROM release_observations" in sql else []
            )
            return cursor

        connection.execute.side_effect = execute
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        result = store.prune_operational_history("cutoff", "cutoff", 100, "now")

        self.assertEqual(1, result["release_observations"])
        query = next(
            call.args[0]
            for call in connection.execute.call_args_list
            if "DELETE FROM release_observations" in call.args[0]
        )
        self.assertIn("deployment.status='running'", query)
        self.assertIn("FOR UPDATE OF observation SKIP LOCKED", query)


class TaskInputMergeTests(unittest.TestCase):
    def test_task_input_patch_is_one_atomic_jsonb_update(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {"id": "task"}
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        store.update_task_input("task", {"delivery": True})

        self.assertEqual(1, connection.execute.call_count)
        query = connection.execute.call_args.args[0]
        self.assertIn("input_json=input_json || %s::jsonb", query)
        self.assertIn("RETURNING id", query)

    def test_delivery_resume_release_is_bound_to_its_outbox(self):
        connection = mock.Mock()
        connection.execute.return_value.rowcount = 0
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertFalse(store.release_review_delivery_resume("task", "delivery:old"))

        query = connection.execute.call_args.args[0]
        self.assertIn("input_json->>'_delivery_resume_outbox_id'=%s", query)
        self.assertEqual("delivery:old", connection.execute.call_args.args[1][-1])


class TaskProgressFenceTests(unittest.TestCase):
    def test_stale_or_duplicate_active_transition_is_a_noop(self):
        event = TraceEvent(3, TaskState.PLANNING, "late planning", utc_now())
        for current in (TaskState.PLANNING, TaskState.EXECUTING, TaskState.REVIEWING):
            with self.subTest(current=current):
                connection = mock.Mock()
                connection.execute.return_value.fetchone.return_value = {
                    "state": current.value,
                    "cancel_requested": False,
                }
                manager = mock.MagicMock()
                manager.__enter__.return_value = connection
                store = object.__new__(PostgresTaskStore)
                store._connect = mock.Mock(return_value=manager)

                self.assertTrue(store.transition("task", event))
                self.assertEqual(1, connection.execute.call_count)


class FallbackTests(unittest.TestCase):
    def test_connect_falls_back_to_per_call_connection(self):
        store = _store_with_pool(None)
        result = store._connect()
        self.assertIs(store.psycopg.sentinel, result)
        self.assertEqual(1, store.psycopg.connect_calls)
        self.assertEqual(5, store.psycopg.connect_timeout)
        self.assertEqual("-c statement_timeout=120000 -c timezone=UTC", store.psycopg.options)

    def test_pool_stats_none_without_pool(self):
        store = _store_with_pool(None)
        self.assertIsNone(store.pool_stats())

    def test_pool_initialization_failure_never_falls_back_to_unbounded_connections(self):
        with (
            mock.patch(
                "psycopg_pool.ConnectionPool", side_effect=PoolExhausted("unavailable")
            ) as backend,
            mock.patch.object(PostgresTaskStore, "_init") as initialize,
            self.assertRaises(PoolExhausted),
        ):
            PostgresTaskStore("postgresql://example/db")

        initialize.assert_not_called()
        self.assertEqual(10, backend.call_args.kwargs["kwargs"]["connect_timeout"])
        self.assertEqual(
            "-c statement_timeout=120000 -c timezone=UTC",
            backend.call_args.kwargs["kwargs"]["options"],
        )

        with self.assertRaisesRegex(ValueError, "EVOAGENT_PG_POOL_TIMEOUT"):
            PostgresTaskStore("postgresql://example/db", pool_timeout=float("inf"))

    def test_create_store_rejects_non_postgres_url(self):
        with self.assertRaisesRegex(ValueError, "PostgreSQL URL"):
            create_store("")

    def test_create_store_does_not_migrate_by_default(self):
        with mock.patch("evoagent.postgres_store.PostgresTaskStore") as backend:
            create_store("postgresql://example/db")
        backend.assert_called_once_with(
            "postgresql://example/db", 1, 10, 10.0, 120.0, auto_migrate=False
        )

    def test_create_store_rejects_invalid_pool_bounds_before_connecting(self):
        for pool_min, pool_max, timeout in (
            (-1, 1, 10),
            (2, 1, 10),
            (0, 0, 10),
            (0, 257, 10),
            (False, 1, 10),
            (0, 1.5, 10),
            (0, 1, float("inf")),
        ):
            with (
                self.subTest(values=(pool_min, pool_max, timeout)),
                self.assertRaisesRegex(ValueError, "EVOAGENT_PG_POOL"),
            ):
                create_store("postgresql://example/db", pool_min, pool_max, timeout)

        with self.assertRaisesRegex(ValueError, "EVOAGENT_PG_STATEMENT_TIMEOUT_SECONDS"):
            create_store("postgresql://example/db", statement_timeout_seconds=0)


class PoolStatGaugeTests(unittest.TestCase):
    def test_stat_reader_probes_alternate_keys(self):
        # Mirrors service._register_pool_metrics: tolerate version key drift.
        m = Metrics()
        store = _store_with_pool(FakePool())

        def stat(*keys):
            def read():
                current = store.pool_stats() or {}
                for key in keys:
                    if key in current:
                        return float(current[key])
                return 0.0

            return read

        m.register_gauge_source("pg_pool_waiting", stat("requests_waiting", "requests_queued"))
        self.assertIn("evoagent_pg_pool_waiting 1", m.prometheus())


class SkillVersionQueryTests(unittest.TestCase):
    def test_version_lookup_uses_the_unique_key_without_loading_history(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {
            "skill_name": "llm-review",
            "version": 7,
            "prompt": "review",
        }
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertEqual(7, store.get_skill_version("llm-review", 7)["version"])

        query, params = connection.execute.call_args.args
        self.assertIn("skill_name=%s AND version=%s", query)
        self.assertNotIn("ORDER BY", query)
        self.assertEqual(("llm-review", 7), params)
        for invalid in (True, 0, 1.0):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "positive"):
                store.get_skill_version("llm-review", invalid)
        self.assertEqual(1, connection.execute.call_count)

    def test_prompt_lookup_returns_only_the_latest_matching_version(self):
        connection = mock.Mock()
        connection.execute.return_value.fetchone.return_value = {"version": 3}
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        self.assertEqual(3, store.get_skill_version_by_prompt("llm-review", "prompt")["version"])

        query, params = connection.execute.call_args.args
        self.assertIn("skill_name=%s AND BTRIM(prompt)=%s", query)
        self.assertIn("ORDER BY version DESC LIMIT 1", query)
        self.assertEqual(("llm-review", "prompt"), params)

    def test_version_parent_and_number_are_read_under_one_skill_lock(self):
        active = mock.Mock()
        active.fetchone.return_value = {"version": 4}
        latest = mock.Mock()
        latest.fetchone.return_value = {"version": 5}
        connection = mock.Mock()
        connection.execute.side_effect = [mock.Mock(), active, latest, mock.Mock()]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        version = store.save_skill_version("llm-review", "prompt", 0.9, "approved")

        self.assertEqual(6, version["version"])
        self.assertEqual(1, store._connect.call_count)
        queries = connection.execute.call_args_list
        self.assertIn("pg_advisory_xact_lock", queries[0].args[0])
        self.assertIn("active=TRUE", queries[1].args[0])
        self.assertIn("MAX(version)", queries[2].args[0])
        self.assertEqual(4, queries[3].args[1][5])

    def test_invalid_skill_version_values_fail_before_store_access(self):
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock()
        invalid = (
            ("", "prompt", 0.5, "approved"),
            (" llm-review", "prompt", 0.5, "approved"),
            ("llm-review", "x" * 12_001, 0.5, "approved"),
            ("llm-review", "prompt", float("nan"), "approved"),
            ("llm-review", "prompt", True, "approved"),
            ("llm-review", "prompt", 0.5, "unknown"),
            ("llm-review", "prompt", 0.5, None),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                store.save_skill_version(*values)
        store._connect.assert_not_called()


class DashboardStatsTests(unittest.TestCase):
    def test_failure_rate_excludes_active_retry_admissions_and_pending_tasks(self):
        connection = mock.Mock()
        task_counts = mock.Mock()
        task_counts.fetchone.return_value = {"total": 3, "success": 1, "failed": 1}
        failure_cases = mock.Mock()
        failure_cases.fetchone.return_value = {"n": 0}
        skills = mock.Mock()
        skills.fetchone.return_value = {"n": 0}
        connection.execute.side_effect = (task_counts, failure_cases, skills)
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        stats = store.dashboard_stats("tenant-a")

        query = connection.execute.call_args_list[0].args[0]
        self.assertIn("NOT EXISTS", query)
        self.assertIn("admission.active=TRUE", query)
        self.assertEqual(3, stats["tasks_total"])
        self.assertEqual(0.5, stats["success_rate"])

    def test_task_list_derives_retrying_from_active_admission(self):
        created = mock.Mock()
        created.isoformat.return_value = "2026-08-24T00:00:00+00:00"
        connection = mock.Mock()
        connection.execute.return_value.fetchall.return_value = [
            {
                "id": "task-1",
                "state": "FAILED",
                "repository": "org/repo",
                "pull_request": 1,
                "error": "safe failure",
                "created_at": created,
                "updated_at": created,
                "tenant_id": "tenant-a",
                "retrying": True,
            }
        ]
        manager = mock.MagicMock()
        manager.__enter__.return_value = connection
        store = object.__new__(PostgresTaskStore)
        store._connect = mock.Mock(return_value=manager)

        tasks = store.list_tasks(10, "tenant-a")

        query = connection.execute.call_args.args[0]
        self.assertIn("admission.active=TRUE", query)
        self.assertTrue(tasks[0]["retrying"])


if __name__ == "__main__":
    unittest.main()
