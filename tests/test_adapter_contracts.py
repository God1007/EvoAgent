"""Behavioral contracts shared by replaceable infrastructure adapters."""

import os
import tempfile
import threading
import unittest
import uuid

from evoagent.github import GitHubClient
from evoagent.migrations import CURRENT_SCHEMA_VERSION
from evoagent.models import TaskState, TraceEvent
from evoagent.ports import ApplicationStorePort, CodeHostPort, TaskQueuePort
from evoagent.postgres_store import PostgresTaskStore
from evoagent.store import TaskStore, utc_now
from evoagent.task_queue import TaskQueue


class PortSurfaceTests(unittest.TestCase):
    def test_sqlite_and_postgres_expose_the_application_store_port(self):
        with tempfile.TemporaryDirectory() as directory:
            sqlite = TaskStore(os.path.join(directory, "contract.db"))
            self.assertIsInstance(sqlite, ApplicationStorePort)

        # Structural protocol checks do not need a live database connection and
        # catch adapter drift such as a method implemented only by SQLite.
        postgres = object.__new__(PostgresTaskStore)
        self.assertIsInstance(postgres, ApplicationStorePort)

    def test_github_exposes_the_code_host_port(self):
        self.assertIsInstance(GitHubClient(""), CodeHostPort)

    def test_memory_queue_exposes_the_task_queue_port(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        try:
            self.assertIsInstance(queue, TaskQueuePort)
        finally:
            queue.close()


class _StoreBehaviorContract:
    store: ApplicationStorePort

    def unique(self, prefix: str) -> str:
        return "%s-%s" % (prefix, uuid.uuid4().hex)

    def test_task_checkpoint_payload_and_cancellation_contract(self):
        task_id = self.unique("task")
        self.store.ping()
        self.assertEqual(CURRENT_SCHEMA_VERSION, self.store.schema_version())
        self.store.create(task_id, "acme/widgets", 17, {"source": "contract"}, "tenant-a")
        self.store.transition(
            task_id,
            TraceEvent(1, TaskState.PLANNING, "planning", utc_now()),
        )
        self.store.save_checkpoint(task_id, "planning", {"files": 2})
        self.store.save_task_payload(task_id, "--- a/a.py\n+++ b/a.py\n")

        task = self.store.get(task_id, "tenant-a")
        self.assertIsNotNone(task)
        self.assertEqual(TaskState.PLANNING.value, task["state"])
        self.assertEqual({"files": 2}, self.store.load_checkpoints(task_id)["planning"]["state"])
        self.assertEqual("--- a/a.py\n+++ b/a.py\n", self.store.get_task_payload(task_id))
        self.assertTrue(self.store.request_cancel(task_id, "tenant-a"))
        self.assertTrue(self.store.is_cancelled(task_id))

    def test_session_continuity_contract(self):
        repository = "acme/%s" % self.unique("sessions")
        first = self.store.start_session_turn("tenant-a", repository, 31, "head-1", "opened")
        snapshot = {
            "fingerprint": "finding-1",
            "status": "new",
            "path": "app.py",
            "line": 3,
        }
        self.store.complete_session_turn(
            first["session_id"],
            first["turn_id"],
            None,
            [snapshot],
            {"new": 1},
            "head-1",
        )
        second = self.store.start_session_turn("tenant-a", repository, 31, "head-2", "synchronize")

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual([snapshot], second["previous_findings"])
        timeline = self.store.get_session_timeline(first["session_id"], "tenant-a")
        self.assertIsNotNone(timeline)
        self.assertEqual(2, len(timeline["turns"]))

    def test_identity_webhook_and_audit_contract(self):
        username = self.unique("user")
        user_id = self.unique("id")
        self.store.create_user(user_id, username, "hash", "tenant-a", "admin")
        user = self.store.get_user(username)
        self.assertIsNotNone(user)
        self.assertEqual(user_id, user["id"])

        delivery = self.unique("delivery")
        self.assertTrue(self.store.claim_webhook(delivery, "tenant-a", "pull_request", "abc"))
        self.assertFalse(self.store.claim_webhook(delivery, "tenant-a", "pull_request", "abc"))
        self.store.complete_webhook(delivery, None)
        self.assertEqual("tenant-a", self.store.get_webhook(delivery)["tenant_id"])

        resource = self.unique("resource")
        self.store.audit("tenant-a", username, "contract.checked", resource, {"ok": True})
        self.assertTrue(
            any(item["resource"] == resource for item in self.store.list_audit("tenant-a", 100))
        )

    def test_shadow_release_contract(self):
        skill_name = self.unique("review-skill")
        self.store.save_deployment(
            "tenant-a",
            skill_name,
            {
                "stable_version": 1,
                "candidate_version": 2,
                "shadow_percent": 100,
                "min_samples": 1,
                "auto_promote": True,
                "max_disagreement_rate": 0.1,
                "status": "running",
            },
        )
        result = self.store.record_shadow_observation(
            "tenant-a",
            skill_name,
            self.unique("task"),
            "stable",
            {"finding_keys": ["a"]},
            {"finding_keys": ["a"]},
            0.0,
        )
        self.assertIsNotNone(result)
        self.assertEqual("promoted", result["status"])
        self.assertEqual("promoted", self.store.get_deployment("tenant-a", skill_name)["status"])

    def test_transactional_outbox_and_effect_receipt_contract(self):
        task_id = self.unique("outbox-task")
        self.store.create_review_task(
            task_id,
            "acme/widgets",
            19,
            {"source": "contract"},
            "tenant-a",
            "--- a/a.py\n+++ b/a.py\n",
            {"task_id": task_id, "repository": "acme/widgets"},
        )
        messages = self.store.claim_outbox("contract-worker", 10, 30, 5)
        self.assertEqual([task_id], [item["message_key"] for item in messages])
        self.assertTrue(self.store.mark_outbox_published(messages[0]["id"], "contract-worker"))

        effect_key = self.unique("effect")
        self.assertEqual(
            "acquired", self.store.claim_effect(effect_key, "contract-worker", 30)["status"]
        )
        self.assertTrue(
            self.store.complete_effect(effect_key, "contract-worker", {"published": True})
        )
        cached = self.store.claim_effect(effect_key, "another-worker", 30)
        self.assertEqual({"published": True}, cached["result"])


class SQLiteStoreContractTests(_StoreBehaviorContract, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = TaskStore(os.path.join(self.directory.name, "contract.db"))

    def tearDown(self):
        self.directory.cleanup()


@unittest.skipUnless(
    os.getenv("EVOAGENT_TEST_POSTGRES_URL"),
    "EVOAGENT_TEST_POSTGRES_URL is not configured",
)
class PostgreSQLStoreContractTests(_StoreBehaviorContract, unittest.TestCase):
    def setUp(self):
        self.store = PostgresTaskStore(
            os.environ["EVOAGENT_TEST_POSTGRES_URL"],
            pool_min=0,
            pool_max=2,
        )

    def tearDown(self):
        self.store.close()


class _QueueBehaviorContract:
    redis_url = ""
    expected_backend = ""
    expected_durable = False

    def test_delivery_and_shutdown_contract(self):
        received: list[str] = []
        delivered = threading.Event()
        message_id = self.unique("message")

        def handler(payload):
            if payload.get("message_id") == message_id:
                received.append(message_id)
                delivered.set()

        queue = TaskQueue(handler, workers=1, redis_url=self.redis_url)
        try:
            self.assertIsInstance(queue, TaskQueuePort)
            self.assertEqual(self.expected_backend, queue.backend)
            self.assertEqual(self.expected_durable, queue.durable)
            health = queue.health()
            self.assertTrue(health["healthy"], health)
            self.assertEqual(self.expected_backend, health["backend"])
            self.assertEqual(
                message_id,
                queue.submit({"message_id": message_id}, message_id=message_id),
            )
            self.assertTrue(delivered.wait(5))
            self.assertEqual([message_id], received)
            self.assertTrue(queue.drain(2))
        finally:
            queue.close(2)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            queue.submit({"message_id": "late"})

    @staticmethod
    def unique(prefix: str) -> str:
        return "%s-%s" % (prefix, uuid.uuid4().hex)


class MemoryQueueContractTests(_QueueBehaviorContract, unittest.TestCase):
    expected_backend = "memory-ephemeral"


@unittest.skipUnless(
    os.getenv("EVOAGENT_TEST_REDIS_URL"),
    "EVOAGENT_TEST_REDIS_URL is not configured",
)
class RedisQueueContractTests(_QueueBehaviorContract, unittest.TestCase):
    redis_url = os.getenv("EVOAGENT_TEST_REDIS_URL", "")
    expected_backend = "redis-streams"
    expected_durable = True


if __name__ == "__main__":
    unittest.main()
