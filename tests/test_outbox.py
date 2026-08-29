import threading
import time
import unittest
import uuid
from unittest import mock

from evoagent.metrics import Metrics
from evoagent.outbox import OutboxDispatcher
from evoagent.postgres_store import PostgresTaskStore
from evoagent.task_queue import TaskQueue
from tests.db_support import postgres_store


def _task(
    store: PostgresTaskStore,
    task_id: str,
    outbox_payload=None,
    tenant_id: str = "tenant-a",
):
    store.create_review_task(
        task_id,
        "acme/widgets",
        17,
        {"source": "test"},
        tenant_id,
        "--- a/a.py\n+++ b/a.py\n",
        outbox_payload,
    )


class _ImmediateRetryStore:
    def __init__(self, store: PostgresTaskStore, fail_mark_once: bool = False):
        self.store = store
        self.fail_mark_once = fail_mark_once

    def __getattr__(self, name):
        return getattr(self.store, name)

    def mark_outbox_published(self, message_id, owner):
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RuntimeError("crash after queue publish")
        return self.store.mark_outbox_published(message_id, owner)

    def release_outbox(self, message_id, owner, error, _retry_delay, max_attempts):
        return self.store.release_outbox(message_id, owner, error, 0, max_attempts)


class _FailingQueue:
    def submit(self, _payload, message_id=""):
        raise RuntimeError("queue-token=outbox-secret")


class OutboxHealthTests(unittest.TestCase):
    def test_resource_limits_cannot_be_silently_clamped(self):
        for options, message in (
            ({"poll_seconds": 0}, "poll interval"),
            ({"poll_seconds": True}, "poll interval"),
            ({"poll_seconds": float("nan")}, "poll interval"),
            ({"batch_size": 0}, "batch size"),
            ({"batch_size": 501}, "batch size"),
            ({"batch_size": True}, "batch size"),
            ({"lease_seconds": 0}, "lease"),
            ({"lease_seconds": True}, "lease"),
            ({"lease_seconds": float("nan")}, "lease"),
            ({"max_attempts": 0}, "attempts"),
            ({"max_attempts": True}, "attempts"),
            ({"autostart": "false"}, "autostart"),
        ):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, message):
                    OutboxDispatcher(mock.Mock(), mock.Mock(), **options)

    def test_release_failure_does_not_abandon_the_rest_of_the_batch(self):
        store = mock.Mock()
        store.claim_outbox.return_value = [
            {
                "id": message_id,
                "topic": "review",
                "message_key": message_id,
                "payload": {},
                "attempts": 1,
            }
            for message_id in ("bad", "good")
        ]
        store.release_outbox.side_effect = RuntimeError("database unavailable")
        store.mark_outbox_published.return_value = True
        queue = mock.Mock()
        queue.submit.side_effect = (RuntimeError("queue unavailable"), None)
        dispatcher = OutboxDispatcher(store, queue, autostart=False)
        captured = Metrics()

        with mock.patch("evoagent.outbox.metrics", captured):
            self.assertEqual(1, dispatcher.dispatch_once())

        self.assertEqual(2, queue.submit.call_count)
        self.assertTrue(dispatcher.last_error)
        output = captured.prometheus()
        self.assertIn("evoagent_outbox_publish_failures_total 1.0", output)
        self.assertIn("evoagent_outbox_dispatch_failures_total 1.0", output)

    def test_error_tracks_the_latest_complete_dispatch_pass(self):
        store = mock.Mock()
        store.claim_outbox.side_effect = [
            [
                {
                    "id": "bad",
                    "topic": "review",
                    "message_key": "bad",
                    "payload": {},
                    "attempts": 1,
                },
                {
                    "id": "good",
                    "topic": "review",
                    "message_key": "good",
                    "payload": {},
                    "attempts": 1,
                },
            ],
            [],
        ]
        store.release_outbox.return_value = False
        store.mark_outbox_published.return_value = True
        queue = mock.Mock()
        queue.submit.side_effect = [RuntimeError("secret"), None]
        dispatcher = OutboxDispatcher(store, queue, autostart=False)
        captured = Metrics()

        with mock.patch("evoagent.outbox.metrics", captured):
            self.assertEqual(1, dispatcher.dispatch_once())
            self.assertTrue(dispatcher.last_error)
            self.assertEqual(0, dispatcher.dispatch_once())
        self.assertEqual("", dispatcher.last_error)
        self.assertIn("evoagent_outbox_lease_conflicts_total 1.0", captured.prometheus())

    def test_retry_backoff_caps_before_exponentiating_attempts(self):
        store = mock.Mock()
        store.claim_outbox.return_value = [
            {
                "id": "poison",
                "topic": "review",
                "message_key": "poison",
                "payload": {},
                "attempts": 1_000_000,
            }
        ]
        store.release_outbox.return_value = True
        queue = mock.Mock()
        queue.submit.side_effect = RuntimeError("queue unavailable")
        dispatcher = OutboxDispatcher(store, queue, autostart=False)

        self.assertEqual(0, dispatcher.dispatch_once())

        self.assertEqual(30.0, store.release_outbox.call_args.args[3])


class TransactionalOutboxTests(unittest.TestCase):
    def setUp(self):
        self.store = postgres_store(self)

    def test_task_payload_and_outbox_rollback_as_one_transaction(self):
        with self.store._connect() as conn:
            conn.execute(
                """CREATE FUNCTION reject_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'fault between task commit and outbox'; END $$"""
            )
            conn.execute(
                "CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox_messages "
                "FOR EACH ROW EXECUTE FUNCTION reject_outbox()"
            )
        self.addCleanup(self._drop_reject_outbox)
        task_id = uuid.uuid4().hex
        with self.assertRaisesRegex(Exception, "fault between"):
            _task(self.store, task_id, {"task_id": task_id})

        self.assertIsNone(self.store.get(task_id))
        self.assertIsNone(self.store.get_task_payload(task_id))
        self.assertEqual(0, self.store.outbox_stats()["total"])

    def _drop_reject_outbox(self):
        with self.store._connect() as conn:
            conn.execute("DROP TRIGGER IF EXISTS reject_outbox ON outbox_messages")
            conn.execute("DROP FUNCTION IF EXISTS reject_outbox()")

    def test_committed_message_is_delivered_after_dispatcher_starts(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id, "repository": "acme/widgets"})
        delivered = threading.Event()
        seen = []
        queue = TaskQueue(lambda payload: (seen.append(payload["task_id"]), delivered.set()))
        dispatcher = OutboxDispatcher(self.store, queue, autostart=False)
        try:
            self.assertEqual(1, self.store.outbox_stats()["pending"])
            self.assertGreaterEqual(self.store.outbox_stats()["oldest_age_seconds"], 0.0)
            self.assertEqual(1, dispatcher.dispatch_once())
            self.assertTrue(delivered.wait(2))
            self.assertEqual([task_id], seen)
            self.assertEqual(0, self.store.outbox_stats()["pending"])
            self.assertEqual(0.0, self.store.outbox_stats()["oldest_age_seconds"])
        finally:
            dispatcher.close()
            queue.close(2)

    def test_outbox_age_is_nonnegative_when_writer_clock_is_ahead_of_database(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id})
        for offset in (600, -600):
            with self.subTest(writer_clock_offset=offset):
                with self.store._connect() as conn:
                    conn.execute(
                        "UPDATE outbox_messages SET created_at="
                        "CURRENT_TIMESTAMP + %s * INTERVAL '1 second' WHERE message_key=%s",
                        (offset, task_id),
                    )
                stats = self.store.outbox_stats()
                self.assertEqual(1, stats["pending"])
                if offset > 0:
                    self.assertEqual(0.0, stats["oldest_age_seconds"])
                else:
                    self.assertGreaterEqual(stats["oldest_age_seconds"], 600.0)

    def test_crash_after_publish_retries_without_duplicate_queue_delivery(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id})
        delivered = threading.Event()
        seen = []
        queue = TaskQueue(lambda payload: (seen.append(payload["task_id"]), delivered.set()))
        wrapped = _ImmediateRetryStore(self.store, fail_mark_once=True)
        dispatcher = OutboxDispatcher(wrapped, queue, autostart=False)
        try:
            self.assertEqual(0, dispatcher.dispatch_once())
            self.assertTrue(delivered.wait(2))
            self.assertEqual(1, dispatcher.dispatch_once())
            time.sleep(0.05)
            self.assertEqual([task_id], seen)
            self.assertEqual(0, self.store.outbox_stats()["pending"])
        finally:
            dispatcher.close()
            queue.close(2)

    def test_expired_publishing_lease_is_reclaimed(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id})
        first = self.store.claim_outbox("dead-worker", 1, 0, 20)
        self.assertEqual(1, len(first))
        time.sleep(0.01)
        second = self.store.claim_outbox("replacement-worker", 1, 30, 20)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(2, second[0]["attempts"])

    def test_expired_final_attempt_is_moved_to_dead(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id})
        self.assertEqual(1, len(self.store.claim_outbox("dead-worker", 1, 0, 1)))
        time.sleep(0.01)

        self.assertEqual([], self.store.claim_outbox("replacement-worker", 1, 30, 1))

        stats = self.store.outbox_stats()
        self.assertEqual(0, stats["publishing"])
        self.assertEqual(1, stats["dead"])
        self.assertRegex(
            self.store.list_outbox("dead")[0]["last_error"],
            r"^outbox dispatch failed \[type=unknown; ref=[0-9a-f]{16}\]$",
        )

    def test_publish_attempt_budget_moves_poison_message_to_dead(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id})
        wrapped = _ImmediateRetryStore(self.store)
        dispatcher = OutboxDispatcher(
            wrapped,
            _FailingQueue(),
            max_attempts=2,
            autostart=False,
        )
        dispatcher.dispatch_once()
        dispatcher.dispatch_once()

        stats = self.store.outbox_stats()
        self.assertEqual(1, stats["dead"])
        self.assertEqual(0, stats["pending"])
        dead = self.store.list_outbox("dead")
        self.assertEqual("review:" + task_id, dead[0]["id"])
        self.assertRegex(
            dead[0]["last_error"],
            r"^outbox dispatch failed \[type=builtins\.RuntimeError; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn("outbox-secret", str(dead))
        self.assertNotIn("outbox-secret", dispatcher.last_error)
        self.assertTrue(self.store.requeue_outbox(dead[0]["id"], "tenant-a", "operator"))
        self.assertEqual(1, self.store.outbox_stats()["pending"])
        replay_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["resource"] == dead[0]["id"]
        )
        self.assertEqual("outbox.replay", replay_audit["action"])
        self.assertEqual({"replayed": True}, replay_audit["detail"])

    def test_outbox_management_is_tenant_scoped(self):
        _task(self.store, "task-a", {"task_id": "task-a", "tenant_id": "tenant-b"})
        _task(
            self.store,
            "task-b",
            {"task_id": "task-b", "tenant_id": "tenant-a"},
            "tenant-b",
        )
        with self.store._connect() as conn:
            conn.execute("UPDATE outbox_messages SET status='dead'")

        self.assertEqual(
            ["review:task-a"],
            [item["id"] for item in self.store.list_outbox("dead", 100, "tenant-a")],
        )
        self.assertFalse(self.store.requeue_outbox("review:task-b", "tenant-a", "operator"))
        self.assertTrue(self.store.requeue_outbox("review:task-a", "tenant-a", "operator"))

    def test_effect_receipt_supports_exclusion_recovery_and_cached_result(self):
        key = "fix-pr:tenant:task"
        self.assertEqual("acquired", self.store.claim_effect(key, "worker-a", 30)["status"])
        self.assertEqual("busy", self.store.claim_effect(key, "worker-b", 30)["status"])
        self.assertTrue(self.store.renew_effect(key, "worker-a", 30))
        self.assertTrue(self.store.release_effect(key, "worker-a", "worker crashed"))
        self.assertEqual("acquired", self.store.claim_effect(key, "worker-b", 30)["status"])
        self.assertFalse(self.store.renew_effect(key, "worker-a", 30))
        result = {"branch": "evoagent/fix", "draft_pull_request": {"number": 7}}
        self.assertTrue(self.store.complete_effect(key, "worker-b", result))
        cached = self.store.claim_effect(key, "worker-c", 30)
        self.assertEqual("completed", cached["status"])
        self.assertEqual(result, cached["result"])


if __name__ == "__main__":
    unittest.main()
