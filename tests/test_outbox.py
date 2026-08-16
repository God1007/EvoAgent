import os
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid

from evoagent.outbox import OutboxDispatcher
from evoagent.store import TaskStore
from evoagent.task_queue import TaskQueue


def _task(store: TaskStore, task_id: str, outbox_payload=None):
    store.create_review_task(
        task_id,
        "acme/widgets",
        17,
        {"source": "test"},
        "tenant-a",
        "--- a/a.py\n+++ b/a.py\n",
        outbox_payload,
    )


class _ImmediateRetryStore:
    def __init__(self, store: TaskStore, fail_mark_once: bool = False):
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
        raise RuntimeError("queue unavailable")


class TransactionalOutboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "outbox.db")
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_task_payload_and_outbox_rollback_as_one_transaction(self):
        with self.store._connect() as conn:
            conn.execute(
                """CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox_messages
                BEGIN SELECT RAISE(ABORT, 'fault between task commit and outbox'); END"""
            )
        task_id = uuid.uuid4().hex
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fault between"):
            _task(self.store, task_id, {"task_id": task_id})

        self.assertIsNone(self.store.get(task_id))
        self.assertIsNone(self.store.get_task_payload(task_id))
        self.assertEqual(0, self.store.outbox_stats()["total"])

    def test_committed_message_is_delivered_after_dispatcher_starts(self):
        task_id = uuid.uuid4().hex
        _task(self.store, task_id, {"task_id": task_id, "repository": "acme/widgets"})
        delivered = threading.Event()
        seen = []
        queue = TaskQueue(lambda payload: (seen.append(payload["task_id"]), delivered.set()))
        dispatcher = OutboxDispatcher(self.store, queue, autostart=False)
        try:
            self.assertEqual(1, self.store.outbox_stats()["pending"])
            self.assertEqual(1, dispatcher.dispatch_once())
            self.assertTrue(delivered.wait(2))
            self.assertEqual([task_id], seen)
            self.assertEqual(0, self.store.outbox_stats()["pending"])
        finally:
            dispatcher.close()
            queue.close(2)

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
        self.assertTrue(self.store.requeue_outbox(dead[0]["id"]))
        self.assertEqual(1, self.store.outbox_stats()["pending"])

    def test_effect_receipt_supports_exclusion_recovery_and_cached_result(self):
        key = "fix-pr:tenant:task"
        self.assertEqual("acquired", self.store.claim_effect(key, "worker-a", 30)["status"])
        self.assertEqual("busy", self.store.claim_effect(key, "worker-b", 30)["status"])
        self.assertTrue(self.store.release_effect(key, "worker-a", "worker crashed"))
        self.assertEqual("acquired", self.store.claim_effect(key, "worker-b", 30)["status"])
        result = {"branch": "evoagent/fix", "draft_pull_request": {"number": 7}}
        self.assertTrue(self.store.complete_effect(key, "worker-b", result))
        cached = self.store.claim_effect(key, "worker-c", 30)
        self.assertEqual("completed", cached["status"])
        self.assertEqual(result, cached["result"])


if __name__ == "__main__":
    unittest.main()
