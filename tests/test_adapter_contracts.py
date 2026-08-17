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

    def test_pull_request_acceptance_is_one_atomic_idempotent_unit(self):
        delivery = self.unique("atomic-delivery")
        task_id = self.unique("atomic-task")
        repository = "acme/" + self.unique("atomic-repository")
        task_payload = {"source": "github-webhook", "diff_pending": True}
        outbox_payload = {
            "repository": repository,
            "pull_request": 41,
            "diff_url": "https://example.invalid/pull.diff",
            "tenant_id": "tenant-a",
        }

        accepted = self.store.accept_pull_request_webhook(
            delivery,
            "tenant-a",
            "payload-hash",
            repository,
            41,
            "head-1",
            "opened",
            task_id,
            task_payload,
            outbox_payload,
        )
        duplicate = self.store.accept_pull_request_webhook(
            delivery,
            "tenant-a",
            "payload-hash",
            repository,
            41,
            "head-1",
            "opened",
            self.unique("must-not-be-created"),
            task_payload,
            outbox_payload,
        )

        self.assertTrue(accepted["accepted"])
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(task_id, duplicate["task_id"])
        self.assertEqual(task_id, self.store.get_webhook(delivery)["task_id"])
        task = self.store.get(task_id, "tenant-a")
        self.assertEqual(accepted["session_id"], task["input"]["session_id"])
        timeline = self.store.get_session_timeline(accepted["session_id"], "tenant-a")
        self.assertEqual(1, len(timeline["turns"]))
        self.assertTrue(
            any(item["message_key"] == task_id for item in self.store.list_outbox("pending", 100))
        )
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.store.accept_pull_request_webhook(
                delivery,
                "tenant-a",
                "different-hash",
                repository,
                41,
                "head-1",
                "opened",
                self.unique("conflict"),
                task_payload,
                outbox_payload,
            )

    def test_pull_request_acceptance_rolls_back_every_record_on_failure(self):
        delivery = self.unique("rollback-delivery")
        task_id = self.unique("rollback-task")
        repository = "acme/" + self.unique("rollback-repository")
        with self.assertRaises(TypeError):
            self.store.accept_pull_request_webhook(
                delivery,
                "tenant-a",
                "payload-hash",
                repository,
                42,
                "head-1",
                "opened",
                task_id,
                {"not_json_serializable": object()},
                {"repository": repository},
            )

        self.assertIsNone(self.store.get_webhook(delivery))
        self.assertIsNone(self.store.get(task_id, "tenant-a"))
        self.assertIsNone(self.store.get_session("tenant-a", repository, 42))

    def test_pull_request_acceptance_recovers_a_legacy_unbound_delivery(self):
        delivery = self.unique("legacy-unbound-delivery")
        task_id = self.unique("recovered-task")
        repository = "acme/" + self.unique("recovered-repository")
        self.assertTrue(
            self.store.claim_webhook(delivery, "tenant-a", "pull_request", "payload-hash")
        )

        recovered = self.store.accept_pull_request_webhook(
            delivery,
            "tenant-a",
            "payload-hash",
            repository,
            44,
            "head-1",
            "opened",
            task_id,
            {"source": "contract", "diff_pending": True},
            {"repository": repository, "pull_request": 44},
        )

        self.assertTrue(recovered["accepted"])
        self.assertEqual(task_id, self.store.get_webhook(delivery)["task_id"])
        self.assertIsNotNone(self.store.get(task_id, "tenant-a"))

    def test_concurrent_duplicate_delivery_creates_exactly_one_task(self):
        delivery = self.unique("concurrent-delivery")
        repository = "acme/" + self.unique("concurrent-repository")
        barrier = threading.Barrier(2)
        results: list[dict] = []
        errors: list[Exception] = []

        def accept(task_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    self.store.accept_pull_request_webhook(
                        delivery,
                        "tenant-a",
                        "payload-hash",
                        repository,
                        43,
                        "head-1",
                        "opened",
                        task_id,
                        {"source": "contract", "diff_pending": True},
                        {"repository": repository, "pull_request": 43},
                    )
                )
            except Exception as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=accept, args=(self.unique("candidate-task"),)) for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, sum(bool(item["accepted"]) for item in results))
        task_ids = {item["task_id"] for item in results}
        self.assertEqual(1, len(task_ids))
        session = self.store.get_session("tenant-a", repository, 43)
        timeline = self.store.get_session_timeline(session["id"], "tenant-a")
        self.assertEqual(1, len(timeline["turns"]))

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

    def test_offline_queue_recovery_contract(self):
        async_task = self.unique("recovery-async")
        sync_task = self.unique("recovery-sync")
        self.store.create_review_task(
            async_task,
            "acme/widgets",
            20,
            {"source": "contract"},
            "tenant-a",
            "--- a/a.py\n+++ b/a.py\n+value = 1\n",
            {"task_id": async_task, "repository": "acme/widgets"},
        )
        self.store.create_review_task(
            sync_task,
            "acme/api",
            21,
            {"source": "contract"},
            "tenant-b",
            "--- a/b.py\n+++ b/b.py\n+value = 2\n",
        )
        candidates = [
            item
            for item in self.store.queue_recovery_candidates(100_001)
            if item["task_id"] in {async_task, sync_task}
        ]
        recovery_id = str(uuid.uuid4())

        result = self.store.stage_queue_recovery(recovery_id, "a" * 64, candidates)

        self.assertEqual(2, result["staged"])
        self.assertFalse(result["already_applied"])
        self.assertEqual("a" * 64, self.store.get_queue_recovery(recovery_id)["plan_sha256"])
        pending = self.store.list_outbox("pending", 500)
        self.assertTrue({async_task, sync_task}.issubset({item["message_key"] for item in pending}))

    def test_versioned_repository_policy_contract(self):
        tenant_id = self.unique("policy-tenant")
        repository = "acme/" + self.unique("policy-repository")
        first_policy = {
            "enabled": True,
            "auto_fix": False,
            "post_review_comments": True,
            "allowed_reviewers": [],
            "allowed_fix_rules": [],
            "allowed_llm_providers": [],
            "allowed_llm_models": [],
            "max_diff_bytes": None,
        }
        first = self.store.save_repository_policy(
            tenant_id, repository, first_policy, "contract-admin"
        )
        self.assertEqual(1, first["version"])
        self.assertTrue(self.store.repository_allowed(tenant_id, repository))
        self.assertFalse(self.store.repository_allowed(tenant_id, repository, True))

        second_policy = {**first_policy, "enabled": False, "auto_fix": True}
        second = self.store.save_repository_policy(
            tenant_id, repository, second_policy, "contract-admin"
        )
        self.assertEqual(2, second["version"])
        self.assertFalse(self.store.repository_allowed(tenant_id, repository))
        current = self.store.get_repository_policy(tenant_id, repository)
        self.assertEqual(second_policy, current["policy"])
        history = self.store.list_repository_policy_versions(tenant_id, repository)
        self.assertEqual([2, 1], [item["version"] for item in history])
        self.assertTrue(
            any(
                item["action"] == "repository-policy.updated" and item["resource"] == repository
                for item in self.store.list_audit(tenant_id, 20)
            )
        )

    def test_model_usage_budget_reservation_contract(self):
        tenant_id = self.unique("model-tenant")
        repository = "acme/" + self.unique("model-repository")

        def record(
            request_id: str,
            tokens: int,
            cost: int,
            created_at: str | None = None,
        ) -> dict:
            return {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "repository": repository,
                "task_id": None,
                "purpose": "review",
                "provider": "contract",
                "model": "model-a",
                "reserved_tokens": tokens,
                "reserved_cost_micros": cost,
                "redactions": 1,
                "request_sha256": "a" * 64,
                "created_at": created_at or utc_now(),
            }

        first = self.unique("model-request")
        second = self.unique("model-request")
        period_start = "2000-01-01T00:00:00+00:00"
        self.assertTrue(
            self.store.reserve_model_usage(record(first, 60, 60), period_start, 100, 100)
        )
        self.assertFalse(
            self.store.reserve_model_usage(record(second, 50, 50), period_start, 100, 100)
        )
        self.assertTrue(self.store.complete_model_usage(first, "success", 10, 10, 20))
        self.assertTrue(
            self.store.reserve_model_usage(record(second, 50, 50), period_start, 100, 100)
        )
        self.assertTrue(self.store.complete_model_usage(second, "failed", 0, 0, 0, "boom"))

        usage = self.store.list_model_usage(tenant_id, repository, 10)
        self.assertEqual({first, second}, {item["request_id"] for item in usage})
        self.assertEqual({"success", "failed"}, {item["status"] for item in usage})
        self.assertTrue(all(item["root_request_id"] == item["request_id"] for item in usage))
        self.assertTrue(all(item["attempt"] == 1 for item in usage))
        self.assertEqual(
            {"contract-model-a"},
            {item["route_id"] for item in usage},
        )

        stale_late = self.unique("model-request")
        stale_manual = self.unique("model-request")
        next_request = self.unique("model-request")
        self.assertTrue(
            self.store.reserve_model_usage(
                record(stale_late, 60, 60, "2000-01-01T01:00:00+00:00"),
                period_start,
            )
        )
        self.assertTrue(
            self.store.reserve_model_usage(
                record(stale_manual, 60, 60, "2000-01-01T01:00:01+00:00"),
                period_start,
            )
        )
        self.assertEqual(
            2,
            self.store.expire_model_usage_reservations("2001-01-01T00:00:00+00:00"),
        )
        self.assertEqual(
            0,
            self.store.expire_model_usage_reservations("2001-01-01T00:00:00+00:00"),
        )
        self.assertTrue(self.store.complete_model_usage(stale_late, "success", 5, 5, 10))
        self.assertFalse(
            self.store.reserve_model_usage(record(next_request, 30, 30), period_start, 100, 100)
        )
        self.assertFalse(
            self.store.reconcile_model_usage(
                tenant_id + "-other", "contract-admin", stale_manual, "failed", 2, 3, 5
            )
        )
        self.assertTrue(
            self.store.reconcile_model_usage(
                tenant_id,
                "contract-admin",
                stale_manual,
                "failed",
                2,
                3,
                5,
                "provider bill verified",
            )
        )
        self.assertTrue(
            self.store.reserve_model_usage(record(next_request, 30, 30), period_start, 100, 100)
        )
        self.assertTrue(self.store.complete_model_usage(next_request, "success", 10, 10, 20))

        final_usage = {
            item["request_id"]: item
            for item in self.store.list_model_usage(tenant_id, repository, 20)
        }
        self.assertEqual("success", final_usage[stale_late]["status"])
        self.assertEqual("failed", final_usage[stale_manual]["status"])
        self.assertEqual(5, final_usage[stale_manual]["cost_micros"])
        self.assertTrue(
            any(
                item["action"] == "model-usage.reconciled" and item["resource"] == stale_manual
                for item in self.store.list_audit(tenant_id, 20)
            )
        )

    def test_concurrent_model_budget_reservations_cannot_overspend(self):
        tenant_id = self.unique("concurrent-model-tenant")
        repository = "acme/" + self.unique("concurrent-model-repository")
        period_start = "2000-01-01T00:00:00+00:00"
        barrier = threading.Barrier(2)
        results: list[bool] = []
        errors: list[Exception] = []

        def reserve(request_id: str) -> None:
            try:
                barrier.wait(timeout=5)
                results.append(
                    self.store.reserve_model_usage(
                        {
                            "request_id": request_id,
                            "tenant_id": tenant_id,
                            "repository": repository,
                            "task_id": None,
                            "purpose": "review",
                            "provider": "contract",
                            "model": "model-a",
                            "reserved_tokens": 60,
                            "reserved_cost_micros": 60,
                            "redactions": 0,
                            "request_sha256": "b" * 64,
                            "created_at": utc_now(),
                        },
                        period_start,
                        100,
                        100,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        workers = [
            threading.Thread(target=reserve, args=(self.unique("concurrent-model-request"),))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual([False, True], sorted(results))
        usage = self.store.list_model_usage(tenant_id, repository, 10)
        self.assertEqual(1, len(usage))
        self.assertEqual("reserved", usage[0]["status"])


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
