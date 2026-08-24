import unittest
import uuid
from unittest import mock

from evoagent.models import ReviewReport, TaskState, TraceEvent
from evoagent.recovery import (
    RECOVERY_MARKER,
    QueueRecoveryError,
    RedisRecoveryTarget,
    build_queue_recovery_plan,
    execute_queue_recovery,
)
from evoagent.time_utils import utc_now
from tests.db_support import postgres_store


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


class RedisRecoveryTargetTests(unittest.TestCase):
    def test_url_cannot_override_recovery_transport_policy(self):
        for query in ("ssl_cert_reqs=none", "socket_timeout=999999"):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "query"):
                RedisRecoveryTarget("rediss://cache.example/0?" + query)

    def test_inspection_is_one_atomic_redis_operation(self):
        target = object.__new__(RedisRecoveryTarget)
        target._client = mock.Mock()
        target._client.eval.return_value = "reserved"

        self.assertEqual("reserved", target.inspect("epoch"))

        target._client.eval.assert_called_once_with(mock.ANY, 1, RECOVERY_MARKER, "epoch")
        target._client.dbsize.assert_not_called()
        target._client.get.assert_not_called()


class QueueRecoveryPlanTests(unittest.TestCase):
    def test_plan_hash_binds_the_selected_outbox_intent(self):
        store = mock.Mock()
        candidate = {
            "task_id": "task-1",
            "tenant_id": "tenant-a",
            "outbox_status": "published",
            "recoverable": True,
            "payload": {"task_id": "task-1", "delivery_only": True},
        }
        store.queue_recovery_candidates.side_effect = [
            [{**candidate, "outbox_id": "delivery-a"}],
            [{**candidate, "outbox_id": "delivery-b"}],
        ]

        first = build_queue_recovery_plan(store, 10)
        second = build_queue_recovery_plan(store, 10)

        self.assertNotEqual(first.plan_sha256, second.plan_sha256)


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = postgres_store(self)

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

    def test_successful_incomplete_delivery_is_reconstructed(self):
        task_id = "successful-delivery"
        delivery_id = "review-delivery:lost"
        self.create_async(task_id)
        self.mark_async_published(task_id)
        self.store.succeed(
            task_id,
            ReviewReport("acme/widgets", 7, "done", "low"),
            TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
        )
        payload = {
            "task_id": task_id,
            "repository": "acme/widgets",
            "pull_request": 7,
            "tenant_id": "tenant-a",
            "delivery_only": True,
        }
        self.assertEqual(
            "resumed",
            self.store.resume_review_delivery(
                task_id, "tenant-a", delivery_id, delivery_id, payload
            )["status"],
        )
        claimed = self.store.claim_outbox("worker", 1, 30, 20)[0]
        self.assertEqual(delivery_id, claimed["id"])
        self.assertTrue(self.store.mark_outbox_published(delivery_id, "worker"))

        recovery_id = str(uuid.uuid4())
        target = FakeRedisRecoveryTarget()
        planned = execute_queue_recovery(self.store, target, recovery_id, "restored")
        applied = execute_queue_recovery(
            self.store,
            target,
            recovery_id,
            "restored",
            apply=True,
            expected_plan_sha256=planned["plan"]["plan_sha256"],
        )

        self.assertEqual(1, planned["plan"]["recoverable"])
        self.assertEqual(1, applied["staging"]["staged"])
        recovered = self.store.list_outbox("pending", 10)[0]
        self.assertEqual("recovery:%s:%s" % (recovery_id, task_id), recovered["message_key"])
        self.assertTrue(recovered["payload"]["delivery_only"])
        self.assertEqual(
            recovered["message_key"],
            self.store.get(task_id)["input"]["_delivery_resume_outbox_id"],
        )

    def test_terminal_race_is_rechecked_inside_staging_transaction(self):
        self.create_async()
        plan = build_queue_recovery_plan(self.store, 10)
        self.store.transition("async-task", TraceEvent(1, TaskState.SUCCESS, "done", utc_now()))

        result = self.store.stage_queue_recovery(
            str(uuid.uuid4()), plan.plan_sha256, list(plan.candidates)
        )

        self.assertEqual(0, result["staged"])
        self.assertEqual(1, result["skipped_terminal"])


if __name__ == "__main__":
    unittest.main()
