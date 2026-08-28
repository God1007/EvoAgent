"""Behavioral contracts shared by replaceable infrastructure adapters."""

import os
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from evoagent.errors import ClientInputError, TenantReviewCapacityError
from evoagent.github import GitHubClient
from evoagent.migrations import CURRENT_SCHEMA_VERSION
from evoagent.models import ReviewReport, TaskState, TraceEvent
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

    def test_repeated_open_alert_refreshes_one_row(self):
        alert_key = self.unique("failure-rate")
        self.store.create_alert("tenant-a", alert_key, "warning", "first")
        created = next(
            item for item in self.store.list_alerts("tenant-a") if item["alert_key"] == alert_key
        )

        self.store.create_alert("tenant-a", alert_key, "critical", "still failing")
        current = [
            item for item in self.store.list_alerts("tenant-a") if item["alert_key"] == alert_key
        ]

        self.assertEqual(1, len(current))
        self.assertEqual("critical", current[0]["severity"])
        self.assertEqual("still failing", current[0]["message"])
        self.assertEqual(created["created_at"], current[0]["created_at"])
        self.assertGreaterEqual(current[0]["updated_at"], created["updated_at"])
        self.assertTrue(self.store.clear_alert("tenant-a", alert_key))
        self.assertFalse(self.store.clear_alert("tenant-a", alert_key))
        self.assertFalse(
            any(item["alert_key"] == alert_key for item in self.store.list_alerts("tenant-a"))
        )

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
        self.store.save_checkpoint(task_id, "planning", {"files": 3}, "completed", 2)
        self.store.save_checkpoint(task_id, "planning", {}, "failed", 3, "late failure")
        self.store.save_checkpoint(task_id, "executing", {}, "failed", 2, "newer failure")
        self.store.save_checkpoint(task_id, "executing", {}, "failed", 1, "stale failure")
        self.store.save_task_payload(task_id, "--- a/a.py\n+++ b/a.py\n")

        task = self.store.get(task_id, "tenant-a")
        self.assertIsNotNone(task)
        self.assertEqual(TaskState.PLANNING.value, task["state"])
        checkpoints = self.store.load_checkpoints(task_id)
        self.assertEqual({"files": 2}, checkpoints["planning"]["state"])
        self.assertEqual("completed", checkpoints["planning"]["status"])
        self.assertEqual(1, checkpoints["planning"]["attempt"])
        self.assertEqual(2, checkpoints["executing"]["attempt"])
        self.assertEqual("--- a/a.py\n+++ b/a.py\n", self.store.get_task_payload(task_id))
        self.assertTrue(self.store.request_cancel(task_id, "tenant-a"))
        self.assertTrue(self.store.is_cancelled(task_id))
        cancellation_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["resource"] == task_id and item["action"] == "task.cancel"
        )
        self.assertTrue(cancellation_audit["detail"]["changed"])
        self.assertFalse(self.store.save_checkpoint(task_id, "planning", {"files": 3}))
        self.assertFalse(
            self.store.record_agent_message(
                task_id,
                {
                    "sender": "late-agent",
                    "recipient": "planner",
                    "kind": "evidence",
                    "content": {"late": True},
                },
            )
        )
        self.assertEqual({"files": 2}, self.store.load_checkpoints(task_id)["planning"]["state"])
        self.assertEqual([], self.store.get(task_id, "tenant-a")["collaboration"])

    def test_task_input_updates_merge_without_replacing_existing_metadata(self):
        task_id = self.unique("task-input")
        self.store.create(
            task_id,
            "acme/widgets",
            17,
            {"reviewer_revision": "revision-1"},
            "tenant-a",
        )

        self.store.update_task_input(task_id, {"_session_recorded": True})
        self.store.update_task_input(task_id, {"_delivery_complete": True})

        self.assertEqual(
            {
                "reviewer_revision": "revision-1",
                "_session_recorded": True,
                "_delivery_complete": True,
            },
            self.store.get(task_id, "tenant-a")["input"],
        )

    def test_active_task_progress_cannot_regress_or_duplicate_trace(self):
        task_id = self.unique("task-progress")
        self.store.create(task_id, "acme/widgets", 17, {}, "tenant-a")
        self.assertTrue(
            self.store.transition(
                task_id,
                TraceEvent(1, TaskState.PLANNING, "planning", utc_now()),
            )
        )
        self.assertTrue(
            self.store.transition(
                task_id,
                TraceEvent(2, TaskState.EXECUTING, "executing", utc_now()),
            )
        )
        self.assertTrue(
            self.store.transition(
                task_id,
                TraceEvent(3, TaskState.PLANNING, "late planning", utc_now()),
            )
        )
        self.assertTrue(
            self.store.transition(
                task_id,
                TraceEvent(4, TaskState.EXECUTING, "duplicate executing", utc_now()),
            )
        )

        task = self.store.get(task_id, "tenant-a")
        self.assertEqual(TaskState.EXECUTING.value, task["state"])
        self.assertEqual(
            [TaskState.PLANNING.value, TaskState.EXECUTING.value],
            [item["state"] for item in task["trace"]],
        )

    def test_success_is_immutable_under_late_worker_writes(self):
        task_id = self.unique("success-terminal")
        self.store.create(task_id, "acme/widgets", 17, {}, "tenant-a")
        self.assertTrue(
            self.store.fail(
                task_id,
                "first attempt failed",
                TraceEvent(1, TaskState.FAILED, "first attempt failed", utc_now()),
            )
        )
        self.store.record_failure_case(
            task_id, "execution_error", {"error": "first attempt failed"}
        )
        self.assertTrue(
            self.store.succeed(
                task_id,
                ReviewReport("acme/widgets", 17, "first", "low"),
                TraceEvent(2, TaskState.SUCCESS, "done", utc_now()),
            )
        )

        self.assertTrue(self.store.save_checkpoint(task_id, "late", {"value": 1}))
        self.assertTrue(
            self.store.transition(
                task_id,
                TraceEvent(2, TaskState.REVIEWING, "late review", utc_now()),
            )
        )
        self.assertTrue(
            self.store.fail(
                task_id,
                "late failure",
                TraceEvent(3, TaskState.FAILED, "late failure", utc_now()),
            )
        )
        self.assertTrue(
            self.store.succeed(
                task_id,
                ReviewReport("acme/widgets", 17, "late", "high"),
                TraceEvent(4, TaskState.SUCCESS, "late success", utc_now()),
            )
        )
        self.store.record_failure_case(task_id, "execution_error", {"error": "late failure"})
        self.assertTrue(
            self.store.record_failure_case(
                task_id,
                "false_positive",
                {"note": "valid feedback"},
                tenant_id="tenant-a",
                actor="alice",
            )
        )

        task = self.store.get(task_id, "tenant-a")
        self.assertEqual(TaskState.SUCCESS.value, task["state"])
        self.assertEqual("first", task["report"]["summary"])
        self.assertEqual(
            [TaskState.FAILED.value, TaskState.SUCCESS.value],
            [item["state"] for item in task["trace"]],
        )
        self.assertEqual({}, self.store.load_checkpoints(task_id))
        unresolved = self.store.list_failure_cases(True, 100, "tenant-a")
        self.assertEqual(["false_positive"], [item["category"] for item in unresolved])
        execution_errors = [
            item
            for item in self.store.list_failure_cases(False, 100, "tenant-a")
            if item["category"] == "execution_error"
        ]
        self.assertEqual(1, len(execution_errors))
        feedback_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["action"] == "review.feedback" and item["resource"] == task_id
        )
        self.assertEqual("alice", feedback_audit["actor"])
        self.assertEqual({"category": "false_positive"}, feedback_audit["detail"])
        self.assertTrue(execution_errors[0]["resolved"])

    def test_session_continuity_contract(self):
        repository = "acme/%s" % self.unique("sessions")
        first = self.store.start_session_turn("tenant-a", repository, 31, "head-1", "opened")
        snapshot = {
            "fingerprint": "finding-1",
            "status": "new",
            "path": "app.py",
            "line": 3,
        }
        self.assertTrue(
            self.store.complete_session_turn(
                first["session_id"],
                first["turn_id"],
                None,
                [snapshot],
                {"new": 1},
                "head-1",
            )
        )
        self.assertFalse(
            self.store.complete_session_turn(
                first["session_id"],
                first["turn_id"],
                None,
                [snapshot],
                {"new": 1},
                "head-1",
            )
        )
        second = self.store.start_session_turn("tenant-a", repository, 31, "head-2", "synchronize")

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual([snapshot], second["previous_findings"])
        timeline = self.store.get_session_timeline(first["session_id"], "tenant-a")
        self.assertIsNotNone(timeline)
        self.assertEqual(2, len(timeline["turns"]))

    def test_successful_delivery_can_be_republished_without_review_admission(self):
        task_id = self.unique("delivery-resume")
        outbox_id = self.unique("delivery-outbox")
        message_key = self.unique("delivery-message")
        self.store.create_review_task(
            task_id,
            "acme/widgets",
            17,
            {"github_issue_url": "https://api.github.com/repos/acme/widgets/issues/17"},
            "tenant-a",
            "--- a/a.py\n+++ b/a.py\n+value = 1\n",
        )
        self.store.succeed(
            task_id,
            ReviewReport("acme/widgets", 17, "done", "low"),
            TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
        )

        resumed = self.store.resume_review_delivery(
            task_id,
            "tenant-a",
            outbox_id,
            message_key,
            {
                "task_id": task_id,
                "repository": "acme/widgets",
                "pull_request": 17,
                "tenant_id": "tenant-a",
            },
        )

        self.assertEqual("resumed", resumed["status"])
        resume_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["resource"] == task_id and item["action"] == "task.resume"
        )
        self.assertEqual("resumed", resume_audit["detail"]["status"])
        self.assertEqual(
            "active",
            self.store.resume_review_delivery(
                task_id,
                "tenant-a",
                self.unique("unused-delivery-outbox"),
                self.unique("unused-delivery-message"),
                {"task_id": task_id},
            )["status"],
        )
        self.assertEqual(
            [message_key],
            [
                item["message_key"]
                for item in self.store.list_outbox("pending", 500)
                if item["id"] == outbox_id
            ],
        )
        claimed = next(
            item
            for item in self.store.claim_outbox("delivery-worker", 500, 30, 1)
            if item["id"] == outbox_id
        )
        self.assertTrue(
            self.store.release_outbox(
                claimed["id"],
                "delivery-worker",
                "outbox dispatch failed",
                0,
                1,
            )
        )
        self.assertEqual(
            "resumed",
            self.store.resume_review_delivery(
                task_id,
                "tenant-a",
                self.unique("recovered-delivery-outbox"),
                self.unique("recovered-delivery-message"),
                {"task_id": task_id},
            )["status"],
        )
        self.assertEqual(0, self.store.tenant_review_admission_stats("tenant-a")["active"])

    def test_operational_retention_preserves_live_and_continuity_anchors(self):
        old = "2000-01-01T00:00:00+00:00"
        cutoff = "2999-01-01T00:00:00+00:00"
        pruned_at = "2030-01-01T00:00:00+00:00"

        terminal_task = self.unique("retention-terminal")
        self.store.create_review_task(
            terminal_task,
            "acme/history",
            1,
            {},
            "tenant-a",
            "private source",
            {"task_id": terminal_task, "tenant_id": "tenant-a"},
        )
        self.store.save_checkpoint(terminal_task, "planning", {"diff": "private source"})
        self.store.record_agent_message(
            terminal_task,
            {
                "sender": "planner",
                "recipient": "reviewer",
                "kind": "evidence",
                "content": {"diff": "private source"},
            },
        )
        self.store.transition(
            terminal_task,
            TraceEvent(1, TaskState.PLANNING, "planning", old),
        )
        self.store.cancel(
            terminal_task,
            TraceEvent(2, TaskState.CANCELLED, "cancelled", old),
        )
        failed_task = self.unique("retention-failed")
        self.store.create(failed_task, "acme/history", 3, {}, "tenant-a")
        self.store.save_task_payload(failed_task, "recoverable source")
        self.store.fail(
            failed_task,
            "review execution failed [type=unknown; ref=1111111111111111]",
            TraceEvent(1, TaskState.FAILED, "failed", old),
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
        self.assertEqual(pruned_at, terminal["input"]["execution_artifacts_pruned_at"])
        self.assertIsNone(self.store.get_task_payload(terminal_task))
        self.assertEqual({}, self.store.load_checkpoints(terminal_task))
        self.assertEqual([], terminal["collaboration"])
        self.assertEqual("recoverable source", self.store.get_task_payload(failed_task))
        self.assertEqual(1, protected["execution_tasks"])
        self.assertEqual(1, protected["task_payloads"])
        self.assertEqual(1, protected["checkpoints"])
        self.assertEqual(1, protected["agent_messages"])
        self.assertEqual(1, protected["outbox_messages"])
        self.assertNotIn(
            "review:" + terminal_task,
            [item["id"] for item in self.store.list_outbox("pending", 500)],
        )
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
            {
                "trace_events": 0,
                "execution_tasks": 0,
                "task_payloads": 0,
                "checkpoints": 0,
                "agent_messages": 0,
                "outbox_messages": 0,
                "effect_receipts": 0,
                "webhook_deliveries": 0,
                "release_observations": 0,
                "session_turns": 0,
                "session_findings": 0,
            },
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
        self.assertTrue(self.store.create_user(user_id, username, "hash", "tenant-a", "admin"))
        user = self.store.get_user(username)
        self.assertIsNotNone(user)
        self.assertEqual(user_id, user["id"])
        self.assertFalse(
            self.store.create_user(
                self.unique("replacement-id"),
                username,
                "replacement-hash",
                "tenant-b",
                "platform_admin",
            )
        )
        user = self.store.get_user(username)
        self.assertEqual("hash", user["password_hash"])
        self.assertEqual(0, user["credential_version"])
        self.assertEqual([{"tenant_id": "tenant-a", "role": "admin"}], user["memberships"])
        self.assertTrue(
            self.store.change_user_password(user_id, "hash", "new-hash", username, "tenant-a")
        )
        self.assertFalse(
            self.store.change_user_password(user_id, "hash", "stale-write", username, "tenant-a")
        )
        user = self.store.get_user(username)
        self.assertEqual("new-hash", user["password_hash"])
        self.assertEqual(1, user["credential_version"])
        password_audit = next(
            item for item in self.store.list_audit("tenant-a", 100) if item["resource"] == user_id
        )
        self.assertEqual("auth.password-change", password_audit["action"])
        self.assertEqual({}, password_audit["detail"])
        managed_id = self.unique("managed-id")
        self.assertTrue(
            self.store.create_user(
                managed_id,
                self.unique("managed-user"),
                "managed-hash",
                "tenant-a",
                "auditor",
                username,
            )
        )
        managed_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["resource"] == managed_id
        )
        self.assertEqual("auth.user-create", managed_audit["action"])
        self.assertEqual("auditor", managed_audit["detail"]["role"])
        self.assertNotIn("password", managed_audit["detail"])
        self.assertTrue(self.store.set_user_active(managed_id, False, username, "tenant-a"))
        self.assertFalse(self.store.get_user(managed_audit["detail"]["username"])["active"])
        self.assertTrue(self.store.set_user_active(managed_id, True, username, "tenant-a"))
        self.assertEqual(
            2, self.store.get_user(managed_audit["detail"]["username"])["credential_version"]
        )

        platform_id = self.unique("platform-id")
        self.assertTrue(
            self.store.create_user(
                platform_id,
                self.unique("platform-user"),
                "platform-hash",
                "tenant-a",
                "platform_admin",
            )
        )
        with self.assertRaisesRegex(ClientInputError, "last active platform"):
            self.store.set_user_active(platform_id, False, username, "tenant-a")
        second_platform_id = self.unique("platform-id")
        self.assertTrue(
            self.store.create_user(
                second_platform_id,
                self.unique("platform-user"),
                "platform-hash",
                "tenant-a",
                "platform_admin",
            )
        )
        self.assertTrue(self.store.set_user_active(platform_id, False, username, "tenant-a"))

        delivery = self.unique("delivery")
        self.assertTrue(self.store.claim_webhook(delivery, "tenant-a", "pull_request", "abc"))
        self.assertFalse(self.store.claim_webhook(delivery, "tenant-a", "pull_request", "abc"))
        self.store.complete_webhook(delivery, None)
        self.assertEqual("tenant-a", self.store.get_webhook(delivery)["tenant_id"])
        protected = self.store.prune_operational_history(
            "1900-01-01T00:00:00+00:00",
            "1900-01-01T00:00:00+00:00",
            100,
            utc_now(),
        )
        self.assertEqual(0, protected["webhook_deliveries"])
        pruned = self.store.prune_operational_history(
            "2999-01-01T00:00:00+00:00",
            "2999-01-01T00:00:00+00:00",
            100,
            utc_now(),
        )
        self.assertEqual(1, pruned["webhook_deliveries"])
        self.assertIsNone(self.store.get_webhook(delivery))

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

    def test_pull_request_end_cancels_active_turns_and_reopens_cleanly(self):
        repository = "acme/" + self.unique("lifecycle-repository")
        task_payload = {"source": "github-webhook", "diff_pending": True}

        running = self.store.accept_pull_request_webhook(
            self.unique("opened-delivery"),
            "tenant-a",
            "opened-hash",
            repository,
            45,
            "head-1",
            "opened",
            self.unique("running-task"),
            task_payload,
            {"repository": repository, "pull_request": 45},
        )
        self.store.transition(
            running["task_id"],
            TraceEvent(1, TaskState.PLANNING, "planning", utc_now()),
        )
        pending = self.store.accept_pull_request_webhook(
            self.unique("sync-delivery"),
            "tenant-a",
            "sync-hash",
            repository,
            45,
            "head-2",
            "synchronize",
            self.unique("pending-task"),
            task_payload,
            {"repository": repository, "pull_request": 45},
        )
        ended_delivery = self.unique("ended-delivery")

        ended = self.store.finish_pull_request_webhook(
            ended_delivery,
            "tenant-a",
            "ended-hash",
            repository,
            45,
            "draft",
        )
        duplicate = self.store.finish_pull_request_webhook(
            ended_delivery,
            "tenant-a",
            "ended-hash",
            repository,
            45,
            "draft",
        )

        self.assertEqual(1, ended["cancelled"])
        self.assertEqual(1, ended["cancel_requested"])
        self.assertFalse(duplicate["accepted"])
        self.assertEqual("draft", self.store.get_session("tenant-a", repository, 45)["status"])
        self.assertTrue(self.store.get(running["task_id"], "tenant-a")["cancel_requested"])
        pending_task = self.store.get(pending["task_id"], "tenant-a")
        self.assertEqual(TaskState.CANCELLED.value, pending_task["state"])
        self.assertTrue(pending_task["input"]["_delivery_complete"])

        reopened = self.store.accept_pull_request_webhook(
            self.unique("ready-delivery"),
            "tenant-a",
            "ready-hash",
            repository,
            45,
            "head-2",
            "ready_for_review",
            self.unique("reopened-task"),
            task_payload,
            {"repository": repository, "pull_request": 45},
        )
        self.assertEqual(running["session_id"], reopened["session_id"])
        self.assertEqual("open", self.store.get_session("tenant-a", repository, 45)["status"])

    def test_pull_request_event_time_prevents_stale_reopening(self):
        repository = "acme/" + self.unique("ordered-lifecycle")
        closed_at = datetime(2026, 8, 23, 12, tzinfo=UTC)
        ended = self.store.finish_pull_request_webhook(
            self.unique("closed-delivery"),
            "tenant-a",
            "closed-hash",
            repository,
            46,
            "closed",
            closed_at,
        )

        stale_task = self.unique("stale-task")
        stale = self.store.accept_pull_request_webhook(
            self.unique("stale-delivery"),
            "tenant-a",
            "stale-hash",
            repository,
            46,
            "old-head",
            "synchronize",
            stale_task,
            {"source": "github-webhook", "diff_pending": True},
            {"repository": repository, "pull_request": 46},
            event_at=closed_at - timedelta(seconds=1),
        )

        self.assertTrue(ended["accepted"])
        self.assertTrue(stale["stale"])
        self.assertIsNone(self.store.get(stale_task, "tenant-a"))
        session = self.store.get_session("tenant-a", repository, 46)
        self.assertEqual("closed", session["status"])
        self.assertEqual(closed_at.isoformat(), session["last_webhook_at"])
        self.assertEqual([], self.store.get_session_timeline(session["id"], "tenant-a")["turns"])

        reopened = self.store.accept_pull_request_webhook(
            self.unique("reopened-delivery"),
            "tenant-a",
            "reopened-hash",
            repository,
            46,
            "new-head",
            "reopened",
            self.unique("reopened-task"),
            {"source": "github-webhook", "diff_pending": True},
            {"repository": repository, "pull_request": 46},
            event_at=closed_at + timedelta(seconds=1),
        )
        self.assertEqual(session["id"], reopened["session_id"])
        self.assertEqual("open", self.store.get_session("tenant-a", repository, 46)["status"])

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
                "max_disagreement_rate": 0.5,
                "status": "running",
            },
        )
        first_generation = self.store.get_deployment("tenant-a", skill_name)["generation"]
        self.store.save_deployment(
            "tenant-a",
            skill_name,
            {
                "stable_version": 1,
                "candidate_version": 2,
                "shadow_percent": 100,
                "min_samples": 1,
                "auto_promote": True,
                "max_disagreement_rate": 0.5,
                "status": "running",
            },
        )
        generation = self.store.get_deployment("tenant-a", skill_name)["generation"]
        self.assertEqual(first_generation + 1, generation)
        deployment_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["resource"] == skill_name
        )
        self.assertEqual("deployment.configure", deployment_audit["action"])
        self.assertEqual(generation, deployment_audit["detail"]["generation"])
        self.assertIsNone(
            self.store.record_deployment_result(
                "tenant-a", skill_name, self.unique("stale-task"), True, 2, first_generation
            )
        )
        self.assertIsNone(
            self.store.record_shadow_observation(
                "tenant-a",
                skill_name,
                self.unique("stale-task"),
                "stable",
                {"finding_keys": ["a"]},
                {"finding_keys": ["b"]},
                1.0,
                2,
                first_generation,
            )
        )
        current = self.store.get_deployment("tenant-a", skill_name)
        self.assertEqual(0, current["samples"])
        self.assertEqual(0, current["shadow_samples"])
        self.assertEqual([], self.store.list_release_observations("tenant-a", skill_name))
        other_tenant_task = self.unique("other-tenant-task")
        self.store.create(other_tenant_task, "acme/widgets", 17, {}, "tenant-b")
        self.store.record_shadow_observation(
            "tenant-a",
            skill_name,
            other_tenant_task,
            "stable",
            {"finding_keys": []},
            {"finding_keys": []},
            0.0,
            2,
            generation,
        )
        self.assertEqual(0, self.store.get_deployment("tenant-a", skill_name)["shadow_samples"])
        self.assertEqual([], self.store.list_release_observations("tenant-a", skill_name))
        canary_task = self.unique("canary-task")
        self.store.create(canary_task, "acme/widgets", 17, {}, "tenant-a")
        first_result = self.store.record_deployment_result(
            "tenant-a", skill_name, canary_task, False, 2, generation
        )
        duplicate_result = self.store.record_deployment_result(
            "tenant-a", skill_name, canary_task, False, 2, generation
        )
        self.assertEqual(1, first_result["samples"])
        self.assertEqual(1, duplicate_result["samples"])
        self.assertEqual(
            {skill_name: True}, self.store.get(canary_task, "tenant-a")["input"]["_release_results"]
        )
        failed_task = self.unique("failed-task")
        self.store.create(failed_task, "acme/widgets", 17, {}, "tenant-a")
        failed = self.store.record_shadow_observation(
            "tenant-a",
            skill_name,
            failed_task,
            "stable",
            {"finding_keys": []},
            None,
            0.0,
            2,
            generation,
            True,
        )
        self.assertEqual("running", failed["status"])
        self.assertEqual(1, failed["disagreements"])
        duplicate = self.store.record_shadow_observation(
            "tenant-a",
            skill_name,
            failed_task,
            "stable",
            {"finding_keys": []},
            None,
            0.0,
            2,
            generation,
            True,
        )
        self.assertEqual(1, duplicate["shadow_samples"])
        self.assertEqual(1, duplicate["disagreements"])
        self.assertEqual(1, len(self.store.list_release_observations("tenant-a", skill_name)))
        protected = self.store.prune_operational_history(
            "2999-01-01T00:00:00+00:00",
            "2999-01-01T00:00:00+00:00",
            100,
            utc_now(),
        )
        self.assertEqual(0, protected["release_observations"])
        passing_task = self.unique("task")
        self.store.create(passing_task, "acme/widgets", 17, {}, "tenant-a")
        result = self.store.record_shadow_observation(
            "tenant-a",
            skill_name,
            passing_task,
            "stable",
            {"finding_keys": ["a"]},
            {"finding_keys": ["a"]},
            0.0,
            2,
            generation,
        )
        self.assertIsNotNone(result)
        self.assertEqual("promoted", result["status"])
        self.assertEqual(2, result["stable_version"])
        self.assertEqual(0, result["shadow_percent"])
        self.assertEqual("promoted", self.store.get_deployment("tenant-a", skill_name)["status"])
        transition_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["resource"] == skill_name and item["action"] == "deployment.auto-promote"
        )
        self.assertEqual(2, transition_audit["detail"]["candidate_version"])
        self.assertEqual(generation, transition_audit["detail"]["generation"])
        pruned = self.store.prune_operational_history(
            "2999-01-01T00:00:00+00:00",
            "2999-01-01T00:00:00+00:00",
            100,
            utc_now(),
        )
        self.assertEqual(2, pruned["release_observations"])
        self.assertEqual([], self.store.list_release_observations("tenant-a", skill_name))

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
            actor="alice",
        )
        creation_audit = next(
            item
            for item in self.store.list_audit("tenant-a", 100)
            if item["action"] == "review.create" and item["resource"] == "acme/widgets"
        )
        self.assertEqual("alice", creation_audit["actor"])
        self.assertEqual({"async": True}, creation_audit["detail"])
        messages = self.store.claim_outbox("contract-worker", 10, 30, 5)
        self.assertEqual([task_id], [item["message_key"] for item in messages])
        self.assertTrue(self.store.mark_outbox_published(messages[0]["id"], "contract-worker"))

        effect_key = self.unique("effect")
        self.assertEqual(
            "acquired", self.store.claim_effect(effect_key, "contract-worker", 30)["status"]
        )
        self.assertTrue(
            self.store.complete_effect(
                effect_key,
                "contract-worker",
                {"published": True},
                audit_event=(
                    "tenant-a",
                    "contract-worker",
                    "effect.complete",
                    effect_key,
                    {"published": True},
                ),
            )
        )
        cached = self.store.claim_effect(effect_key, "another-worker", 30)
        self.assertEqual({"published": True}, cached["result"])
        self.assertTrue(
            any(
                item["action"] == "effect.complete" and item["resource"] == effect_key
                for item in self.store.list_audit("tenant-a", 100)
            )
        )
        active_key = self.unique("active-effect")
        self.assertEqual(
            "acquired", self.store.claim_effect(active_key, "active-worker", 30)["status"]
        )

        pruned = self.store.prune_operational_history(
            "2999-01-01T00:00:00+00:00",
            "2999-01-01T00:00:00+00:00",
            100,
            utc_now(),
        )

        self.assertEqual(1, pruned["effect_receipts"])
        self.assertEqual(
            "acquired", self.store.claim_effect(effect_key, "new-worker", 30)["status"]
        )
        self.assertEqual("busy", self.store.claim_effect(active_key, "other-worker", 30)["status"])

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
        self.assertTrue(self.store.review_admission_active(first, 1))
        self.assertTrue(self.store.review_admission_active(first))
        self.assertFalse(self.store.review_admission_active(first, True))
        self.assertFalse(self.store.release_review_admission(first, "dead-letter", True))
        self.assertTrue(self.store.review_admission_active(first, 1))

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
        self.assertEqual(0, self.store.dashboard_stats(tenant)["tasks_failed"])
        self.assertEqual(TaskState.FAILED.value, self.store.get(first, tenant)["state"])
        listed = next(item for item in self.store.list_tasks(10, tenant) if item["id"] == first)
        self.assertTrue(listed["retrying"])
        self.assertTrue(self.store.get(first, tenant)["retrying"])
        self.assertTrue(self.store.release_review_admission(first, "dead-letter", 1))
        self.assertEqual(0, self.store.tenant_review_admission_stats(tenant)["active"])
        self.assertEqual(1, self.store.dashboard_stats(tenant)["tasks_failed"])
        listed = next(item for item in self.store.list_tasks(10, tenant) if item["id"] == first)
        self.assertFalse(listed["retrying"])
        self.assertFalse(self.store.get(first, tenant)["retrying"])
        self.assertFalse(self.store.review_admission_active(first, 1))
        self.assertFalse(self.store.review_admission_active(first))

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
        self.assertTrue(self.store.review_admission_active(first, 2))
        self.assertEqual(0, self.store.dashboard_stats(tenant)["tasks_failed"])
        self.assertFalse(self.store.review_admission_active(first, 1))
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
        self.assertFalse(self.store.save_checkpoint(first, "stale", {"value": 1}, generation=1))
        self.assertFalse(
            self.store.record_agent_message(
                first,
                {
                    "sender": "stale-agent",
                    "recipient": "planner",
                    "kind": "evidence",
                    "content": {"value": 1},
                },
                1,
            )
        )
        self.assertFalse(
            self.store.transition(
                first,
                TraceEvent(2, TaskState.REVIEWING, "stale worker", utc_now()),
                1,
            )
        )
        self.assertNotIn("stale", self.store.load_checkpoints(first))
        self.assertEqual([], self.store.get(first, tenant)["collaboration"])
        self.assertFalse(
            self.store.succeed(
                first,
                ReviewReport("acme/noisy", 1, "stale", "low"),
                TraceEvent(2, TaskState.SUCCESS, "stale worker", utc_now()),
                1,
            )
        )
        self.assertTrue(self.store.review_admission_active(first, 2))
        self.assertEqual(TaskState.FAILED.value, self.store.get(first, tenant)["state"])
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
        self.assertTrue(self.store.request_cancel(cancelled, tenant))
        self.assertEqual(TaskState.CANCELLED.value, self.store.get(cancelled, tenant)["state"])
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
            tenant_id, repository.upper(), second_policy, "contract-admin"
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
