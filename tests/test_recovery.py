import os
import tempfile
import unittest
import uuid

from evoagent.models import TaskState, TraceEvent
from evoagent.recovery import (
    QueueRecoveryError,
    RedisRecoveryTarget,
    build_queue_recovery_plan,
    execute_queue_recovery,
)
from evoagent.store import TaskStore, utc_now
from evoagent.task_queue import build_queue_keyspace


class FakeRedisRecoveryTarget:
    def __init__(self, state="empty"):
        self.state = state
        self.marker = ""

    def inspect(self, marker):
        self.marker = marker
        return self.state

    def reserve(self, marker):
        self.marker = marker
        if self.state == "nonempty":
            return "nonempty"
        previous = self.state
        self.state = "reserved"
        return "existing" if previous == "reserved" else "reserved"


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TaskStore(os.path.join(self.directory.name, "recovery.db"))

    def create_async(self, task_id="async-task"):
        self.store.create_review_task(
            task_id,
            "acme/widgets",
            7,
            {"source": "test"},
            "tenant-a",
            "diff --git a/a.py b/a.py\n+value = 1\n",
            {
                "task_id": task_id,
                "repository": "acme/widgets",
                "pull_request": 7,
                "tenant_id": "tenant-a",
                "github_issue_url": "https://api.example/issues/7",
            },
        )

    def mark_async_published(self, task_id="async-task"):
        message = self.store.claim_outbox("worker", 1, 30, 20)[0]
        self.assertEqual("review:" + task_id, message["id"])
        self.assertTrue(self.store.mark_outbox_published(message["id"], "worker"))

    def test_plan_and_apply_restore_published_and_missing_outbox(self):
        self.create_async()
        self.mark_async_published()
        self.store.create_review_task(
            "sync-task",
            "acme/api",
            None,
            {"source": "api"},
            "tenant-b",
            "diff --git a/b.py b/b.py\n+safe = True\n",
        )
        recovery_id = str(uuid.uuid4())
        target = FakeRedisRecoveryTarget()

        planned = execute_queue_recovery(self.store, target, recovery_id, "restored", apply=False)
        applied = execute_queue_recovery(
            self.store,
            target,
            recovery_id,
            "restored",
            apply=True,
            expected_plan_sha256=planned["plan"]["plan_sha256"],
        )
        reapplied = execute_queue_recovery(
            self.store,
            target,
            recovery_id,
            "restored",
            apply=True,
            expected_plan_sha256=planned["plan"]["plan_sha256"],
        )

        self.assertEqual("planned", planned["status"])
        self.assertEqual(
            {"redis_cluster": False, "queue_namespace": "", "keyspace_version": 1},
            planned["redis_topology"],
        )
        self.assertEqual(2, planned["plan"]["recoverable"])
        self.assertEqual("pass", applied["status"])
        self.assertEqual(2, applied["staging"]["staged"])
        self.assertEqual(1, applied["staging"]["preserved_outbox_history"])
        self.assertFalse(applied["staging"]["already_applied"])
        self.assertTrue(reapplied["staging"]["already_applied"])
        pending = self.store.list_outbox("pending", 10)
        self.assertEqual(
            {"async-task", "sync-task"}, {item["payload"]["task_id"] for item in pending}
        )
        async_payload = next(
            item["payload"] for item in pending if item["payload"]["task_id"] == "async-task"
        )
        self.assertEqual("https://api.example/issues/7", async_payload["github_issue_url"])
        self.assertEqual("async-task", self.store.list_outbox("published", 10)[0]["message_key"])
        self.assertIsNotNone(self.store.get_queue_recovery(recovery_id))

    def test_same_epoch_is_idempotent_only_against_same_reserved_redis(self):
        self.create_async()
        recovery_id = str(uuid.uuid4())
        target = FakeRedisRecoveryTarget()
        plan_sha256 = build_queue_recovery_plan(self.store, 10).plan_sha256
        first = execute_queue_recovery(
            self.store,
            target,
            recovery_id,
            "restored",
            apply=True,
            expected_plan_sha256=plan_sha256,
        )
        second = execute_queue_recovery(
            self.store,
            target,
            recovery_id,
            "restored",
            apply=True,
            expected_plan_sha256=plan_sha256,
        )

        self.assertFalse(first["staging"]["already_applied"])
        self.assertTrue(second["staging"]["already_applied"])
        with self.assertRaisesRegex(QueueRecoveryError, "different Redis"):
            execute_queue_recovery(
                self.store,
                FakeRedisRecoveryTarget("empty"),
                recovery_id,
                "restored",
                apply=True,
                expected_plan_sha256=plan_sha256,
            )

    def test_nonempty_redis_and_unrecoverable_tasks_fail_closed(self):
        self.create_async()
        plan_sha256 = build_queue_recovery_plan(self.store, 10).plan_sha256
        with self.assertRaisesRegex(QueueRecoveryError, "not empty"):
            execute_queue_recovery(
                self.store,
                FakeRedisRecoveryTarget("nonempty"),
                str(uuid.uuid4()),
                "restored",
                apply=True,
                expected_plan_sha256=plan_sha256,
            )

        self.store.create_review_task(
            "lost-task", "acme/lost", None, {"diff_pending": True}, "tenant-a"
        )
        with self.assertRaisesRegex(QueueRecoveryError, "valid Outbox payload"):
            execute_queue_recovery(
                self.store,
                FakeRedisRecoveryTarget(),
                str(uuid.uuid4()),
                "restored",
                apply=False,
            )

    def test_apply_is_bound_to_a_reviewed_plan_hash(self):
        self.create_async()
        recovery_id = str(uuid.uuid4())

        with self.assertRaisesRegex(ValueError, "reviewed dry-run"):
            execute_queue_recovery(
                self.store,
                FakeRedisRecoveryTarget(),
                recovery_id,
                "restored",
                apply=True,
            )
        with self.assertRaisesRegex(QueueRecoveryError, "does not match"):
            execute_queue_recovery(
                self.store,
                FakeRedisRecoveryTarget(),
                recovery_id,
                "restored",
                apply=True,
                expected_plan_sha256="0" * 64,
            )

    def test_terminal_or_cancel_requested_tasks_are_not_candidates(self):
        self.create_async("successful")
        self.store.transition("successful", TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))
        self.create_async("cancelled")
        self.store.request_cancel("cancelled", "tenant-a")

        plan = build_queue_recovery_plan(self.store, 10)

        self.assertEqual(0, len(plan.candidates))

    def test_terminal_race_is_rechecked_inside_staging_transaction(self):
        self.create_async()
        plan = build_queue_recovery_plan(self.store, 10)
        self.store.transition("async-task", TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))

        result = self.store.stage_queue_recovery(
            str(uuid.uuid4()), plan.plan_sha256, list(plan.candidates)
        )

        self.assertEqual(0, result["staged"])
        self.assertEqual(1, result["skipped_terminal"])

    def test_v2_recovery_reserves_only_its_queue_namespace(self):
        class FakeNamespaceRedis:
            def __init__(self):
                self.values = {"evoagent:{review:other}:stream": "unrelated"}

            def exists(self, *keys):
                return sum(key in self.values for key in keys)

            def get(self, key):
                return self.values.get(key)

            def eval(self, _script, key_count, *values):
                keys = values[:key_count]
                marker = values[key_count]
                if any(key in self.values for key in keys[1:]):
                    return "nonempty"
                current = self.values.get(keys[0])
                if current is None:
                    self.values[keys[0]] = marker
                    return "reserved"
                return "existing" if current == marker else "nonempty"

        target = object.__new__(RedisRecoveryTarget)
        target.redis_cluster = True
        target.keyspace = build_queue_keyspace("recovery-prod")
        target._client = FakeNamespaceRedis()

        self.assertEqual("empty", target.inspect("reviewed"))
        self.assertEqual("reserved", target.reserve("reviewed"))
        self.assertEqual("reserved", target.inspect("reviewed"))
        target._client.values[target.keyspace.stream] = "occupied"
        self.assertEqual("nonempty", target.inspect("reviewed"))


if __name__ == "__main__":
    unittest.main()
