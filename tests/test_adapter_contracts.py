"""Behavioral contracts shared by replaceable infrastructure adapters."""

import os
import threading
import unittest
import uuid

from evoagent.errors import TenantReviewCapacityError
from evoagent.github import GitHubClient
from evoagent.migrations import CURRENT_SCHEMA_VERSION
from evoagent.models import TaskState, TraceEvent
from evoagent.ports import (
    ApplicationStorePort,
    CodeHostPort,
    TaskQueuePort,
)
from evoagent.postgres_store import PostgresTaskStore
from evoagent.task_queue import TaskQueue
from evoagent.time_utils import utc_now


class PortSurfaceTests(unittest.TestCase):
    def test_postgres_exposes_the_application_store_port(self):
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

    def test_operational_retention_preserves_live_and_continuity_anchors(self):
        old = "2000-01-01T00:00:00+00:00"
        cutoff = "2999-01-01T00:00:00+00:00"
        pruned_at = "2030-01-01T00:00:00+00:00"

        terminal_task = self.unique("retention-terminal")
        self.store.create(terminal_task, "acme/history", 1, {}, "tenant-a")
        self.store.transition(
            terminal_task,
            TraceEvent(1, TaskState.PLANNING, "planning", old),
        )
        self.store.cancel(
            terminal_task,
            TraceEvent(2, TaskState.CANCELLED, "cancelled", old),
        )
        active_task = self.unique("retention-active")
        self.store.create(active_task, "acme/history", 2, {}, "tenant-a")
        self.store.transition(
            active_task,
            TraceEvent(1, TaskState.PLANNING, "planning", old),
        )

        repository = "acme/" + self.unique("retention-session")
        first = self.store.start_session_turn("tenant-a", repository, 7, "head-1", "opened")
        first_snapshot = {"fingerprint": "first", "status": "new", "path": "one.py"}
        self.store.complete_session_turn(
            first["session_id"],
            first["turn_id"],
            None,
            [first_snapshot],
            {"new": 1},
            "head-1",
        )
        second = self.store.start_session_turn("tenant-a", repository, 7, "head-2", "synchronize")
        third = self.store.start_session_turn("tenant-a", repository, 7, "head-3", "synchronize")
        third_snapshot = {"fingerprint": "third", "status": "new", "path": "three.py"}
        self.store.complete_session_turn(
            third["session_id"],
            third["turn_id"],
            None,
            [third_snapshot],
            {"new": 1},
            "head-3",
        )

        protected = self.store.prune_operational_history(cutoff, cutoff, 100, pruned_at)

        self.assertEqual(1, protected["trace_events"])
        self.assertEqual(0, protected["session_turns"])
        terminal = self.store.get(terminal_task, "tenant-a")
        self.assertEqual([TaskState.CANCELLED.value], [item["state"] for item in terminal["trace"]])
        self.assertEqual(pruned_at, terminal["trace_pruned_at"])
        self.assertEqual(1, len(self.store.get(active_task, "tenant-a")["trace"]))
        self.assertEqual(
            [first_snapshot],
            self.store.previous_open_snapshot(first["session_id"], second["turn_id"]),
        )

        second_snapshot = {"fingerprint": "second", "status": "new", "path": "two.py"}
        self.store.complete_session_turn(
            second["session_id"],
            second["turn_id"],
            None,
            [second_snapshot],
            {"new": 1},
            "head-2",
        )
        pruned = self.store.prune_operational_history(cutoff, cutoff, 100, pruned_at)

        self.assertEqual(2, pruned["session_turns"])
        self.assertEqual(2, pruned["session_findings"])
        timeline = self.store.get_session_timeline(first["session_id"], "tenant-a")
        turns = {item["id"]: item for item in timeline["turns"]}
        self.assertFalse(turns[first["turn_id"]]["findings_retained"])
        self.assertFalse(turns[second["turn_id"]]["findings_retained"])
        self.assertEqual([], turns[first["turn_id"]]["findings"])
        self.assertEqual(pruned_at, turns[first["turn_id"]]["findings_pruned_at"])
        self.assertTrue(turns[third["turn_id"]]["findings_retained"])
        self.assertEqual([third_snapshot], turns[third["turn_id"]]["findings"])

        fourth = self.store.start_session_turn("tenant-a", repository, 7, "head-4", "synchronize")
        self.assertEqual([third_snapshot], fourth["previous_findings"])
        self.assertEqual(
            {"trace_events": 0, "session_turns": 0, "session_findings": 0},
            self.store.prune_operational_history(cutoff, cutoff, 100, pruned_at),
        )
        rewritten = {"fingerprint": "first-rewritten", "status": "new", "path": "one.py"}
        self.store.complete_session_turn(
            first["session_id"],
            first["turn_id"],
            None,
            [rewritten],
            {"new": 1},
            "head-1",
        )
        timeline = self.store.get_session_timeline(first["session_id"], "tenant-a")
        first_turn = next(item for item in timeline["turns"] if item["id"] == first["turn_id"])
        self.assertTrue(first_turn["findings_retained"])
        self.assertIsNone(first_turn["findings_pruned_at"])
        self.assertEqual([rewritten], first_turn["findings"])

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

    def test_tenant_review_admission_is_atomic_and_tracks_delivery_lifecycle(self):
        tenant = self.unique("admission-tenant")
        first = self.unique("admission-async")
        self.store.create_review_task(
            first,
            "acme/noisy",
            1,
            {"source": "contract"},
            tenant,
            "--- a/a.py\n+++ b/a.py\n+value = 1\n",
            {"task_id": first, "repository": "acme/noisy", "tenant_id": tenant},
            1,
        )
        self.assertEqual(1, self.store.tenant_review_admission_stats(tenant)["active"])

        rejected = self.unique("admission-rejected")
        with self.assertRaises(TenantReviewCapacityError):
            self.store.create_review_task(
                rejected,
                "acme/noisy",
                2,
                {"source": "contract"},
                tenant,
                None,
                {"task_id": rejected, "repository": "acme/noisy", "tenant_id": tenant},
                1,
            )
        self.assertIsNone(self.store.get(rejected, tenant))
        audit = self.store.list_audit(tenant, 10)
        rejection = next(item for item in audit if item["action"] == "review.capacity-rejected")
        self.assertEqual({"active": 1, "limit": 1, "source": "contract"}, rejection["detail"])

        # An async execution failure remains delivery-managed and therefore
        # keeps its slot through retries. Final DLQ disposition releases it.
        self.store.fail(
            first,
            "review execution failed [type=unknown; ref=0000000000000000]",
            TraceEvent(1, TaskState.FAILED, "failed", utc_now()),
        )
        self.assertEqual(1, self.store.tenant_review_admission_stats(tenant)["active"])
        self.assertTrue(self.store.release_review_admission(first, "dead-letter", 1))
        self.assertEqual(0, self.store.tenant_review_admission_stats(tenant)["active"])

        resume_key = self.unique("resume")
        resumed = self.store.resume_review_task(
            first,
            tenant,
            1,
            "outbox-" + resume_key,
            "message-" + resume_key,
            {"task_id": first, "repository": "acme/noisy", "tenant_id": tenant},
        )
        self.assertEqual("resumed", resumed["status"])
        self.assertEqual(2, resumed["generation"])
        self.assertEqual(
            "active",
            self.store.resume_review_task(
                first,
                tenant,
                1,
                "unused-outbox-" + resume_key,
                "unused-message-" + resume_key,
                {"task_id": first},
            )["status"],
        )
        pending = self.store.list_outbox("pending", 500)
        self.assertEqual(1, sum(item["message_key"] == "message-" + resume_key for item in pending))
        resumed_message = next(
            item for item in pending if item["message_key"] == "message-" + resume_key
        )
        self.assertEqual(2, resumed_message["payload"]["admission_generation"])
        self.assertFalse(self.store.release_review_admission(first, "dead-letter", 1))
        self.assertEqual(1, self.store.tenant_review_admission_stats(tenant)["active"])
        self.assertTrue(self.store.release_review_admission(first, "dead-letter", 2))

        # A synchronous failure is terminal for delivery and releases
        # immediately, unlike the asynchronous retry path above.
        synchronous = self.unique("admission-sync")
        self.store.create_review_task(
            synchronous,
            "acme/sync",
            3,
            {"source": "contract"},
            tenant,
            "--- a/b.py\n+++ b/b.py\n+value = 2\n",
            None,
            1,
        )
        self.store.fail(
            synchronous,
            "review execution failed [type=unknown; ref=1111111111111111]",
            TraceEvent(1, TaskState.FAILED, "failed", utc_now()),
        )
        self.assertEqual(0, self.store.tenant_review_admission_stats(tenant)["active"])

        cancelled = self.unique("admission-cancelled")
        self.store.create_review_task(
            cancelled,
            "acme/cancelled",
            4,
            {"source": "contract"},
            tenant,
            None,
            {"task_id": cancelled, "repository": "acme/cancelled", "tenant_id": tenant},
            1,
        )
        self.store.cancel(
            cancelled,
            TraceEvent(1, TaskState.CANCELLED, "cancelled", utc_now()),
        )
        self.assertEqual(0, self.store.tenant_review_admission_stats(tenant)["active"])

    def test_tenant_review_admission_serializes_concurrent_replicas(self):
        tenant = self.unique("admission-race")
        barrier = threading.Barrier(2)
        admitted: list[str] = []
        rejected: list[str] = []
        errors: list[Exception] = []

        def create(suffix: str) -> None:
            task_id = self.unique("admission-race-" + suffix)
            try:
                barrier.wait(timeout=5)
                self.store.create_review_task(
                    task_id,
                    "acme/race",
                    4,
                    {"source": "contract"},
                    tenant,
                    None,
                    {"task_id": task_id, "repository": "acme/race", "tenant_id": tenant},
                    1,
                )
                admitted.append(task_id)
            except TenantReviewCapacityError:
                rejected.append(task_id)
            except Exception as exc:
                errors.append(exc)

        workers = [threading.Thread(target=create, args=(str(index),)) for index in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(1, len(admitted))
        self.assertEqual(1, len(rejected))
        self.assertEqual(1, self.store.tenant_review_admission_stats(tenant)["active"])

    def test_webhook_capacity_rejection_keeps_retryable_idempotency_claim(self):
        tenant = self.unique("webhook-capacity")
        occupied = self.unique("webhook-occupied")
        self.store.create_review_task(
            occupied,
            "acme/webhook",
            5,
            {"source": "contract"},
            tenant,
            None,
            {"task_id": occupied, "repository": "acme/webhook", "tenant_id": tenant},
            1,
        )
        delivery = self.unique("delivery-capacity")
        task_id = self.unique("webhook-task")
        arguments = (
            delivery,
            tenant,
            "a" * 64,
            "acme/webhook",
            6,
            "head-1",
            "opened",
            task_id,
            {"source": "github-webhook"},
            {"repository": "acme/webhook", "tenant_id": tenant},
            1,
        )
        with self.assertRaises(TenantReviewCapacityError):
            self.store.accept_pull_request_webhook(*arguments)
        claim = self.store.get_webhook(delivery)
        self.assertEqual("a" * 64, claim["payload_sha256"])
        self.assertIsNone(claim["task_id"])
        self.assertIsNone(self.store.get_session(tenant, "acme/webhook", 6))

        self.assertTrue(self.store.release_review_admission(occupied, "dead-letter"))
        accepted = self.store.accept_pull_request_webhook(*arguments)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(task_id, self.store.get_webhook(delivery)["task_id"])

    def test_offline_queue_recovery_contract(self):
        async_task = self.unique("recovery-async")
        sync_task = self.unique("recovery-sync")
        retrying_task = self.unique("recovery-retrying")
        terminal_failure = self.unique("recovery-terminal-failure")
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
        self.store.create_review_task(
            retrying_task,
            "acme/worker",
            22,
            {"source": "contract"},
            "tenant-c",
            "--- a/c.py\n+++ b/c.py\n+value = 3\n",
            {"task_id": retrying_task, "repository": "acme/worker"},
        )
        self.store.fail(
            retrying_task,
            "transient worker failure",
            TraceEvent(1, TaskState.FAILED, "failed", utc_now()),
        )
        self.assertTrue(self.store.release_review_admission(retrying_task, "dead-letter", 1))
        recovery_resume = self.unique("recovery-resume")
        resumed = self.store.resume_review_task(
            retrying_task,
            "tenant-c",
            1,
            "outbox-" + recovery_resume,
            "message-" + recovery_resume,
            {"task_id": retrying_task, "repository": "acme/worker"},
        )
        self.assertEqual(2, resumed["generation"])
        self.store.fail(
            retrying_task,
            "second transient worker failure",
            TraceEvent(2, TaskState.FAILED, "failed", utc_now()),
        )
        self.store.create_review_task(
            terminal_failure,
            "acme/terminal",
            23,
            {"source": "contract"},
            "tenant-d",
            "--- a/d.py\n+++ b/d.py\n+value = 4\n",
        )
        self.store.fail(
            terminal_failure,
            "synchronous terminal failure",
            TraceEvent(1, TaskState.FAILED, "failed", utc_now()),
        )
        candidates = [
            item
            for item in self.store.queue_recovery_candidates(100_001)
            if item["task_id"] in {async_task, sync_task, retrying_task, terminal_failure}
        ]
        candidate_ids = {item["task_id"] for item in candidates}
        self.assertIn(retrying_task, candidate_ids)
        self.assertNotIn(terminal_failure, candidate_ids)
        retrying_candidate = next(item for item in candidates if item["task_id"] == retrying_task)
        self.assertEqual(2, retrying_candidate["payload"]["admission_generation"])
        recovery_id = str(uuid.uuid4())

        result = self.store.stage_queue_recovery(recovery_id, "a" * 64, candidates)

        self.assertEqual(3, result["staged"])
        self.assertFalse(result["already_applied"])
        self.assertEqual("a" * 64, self.store.get_queue_recovery(recovery_id)["plan_sha256"])
        pending = self.store.list_outbox("pending", 500)
        self.assertTrue(
            {async_task, sync_task, retrying_task}.issubset(
                {item["message_key"] for item in pending}
            )
        )

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
        # The CI service database is shared by every contract method. Reset all
        # application tables after migrations so aggregate operations such as
        # outbox claims and retention pruning cannot observe another test's rows.
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
                "AND table_name<>'schema_migrations'"
            ).fetchall()
            if rows:
                sql = self.store.psycopg.sql
                tables = sql.SQL(", ").join(sql.Identifier(row["table_name"]) for row in rows)
                conn.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(tables))

    def tearDown(self):
        self.store.close()


class _QueueBehaviorContract:
    redis_url = ""
    expected_backend = ""
    expected_durable = False
    expected_heartbeat = False

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
                self.expected_heartbeat,
                health["lease_heartbeat_running"],
            )
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
    expected_heartbeat = True


if __name__ == "__main__":
    unittest.main()
