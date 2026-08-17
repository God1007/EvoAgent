import os
import sqlite3
import tempfile
import unittest

from evoagent.application import (
    ModelUsageUseCases,
    PolicyUseCases,
    RepairOptions,
    RepairUseCases,
    ReviewOptions,
    ReviewUseCases,
    SessionUseCases,
    WebhookOptions,
    WebhookUseCases,
)
from evoagent.models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from evoagent.observability import AlertManager, Observability
from evoagent.policy import RepositoryPolicyResolver
from evoagent.rollout import ReleaseManager
from evoagent.store import TaskStore, utc_now


def _finding() -> Finding:
    return Finding(
        rule_id="SEC-EVAL",
        severity=Severity.HIGH,
        title="Dangerous eval",
        explanation="Untrusted code may execute.",
        path="app.py",
        line=1,
        evidence="eval(value)",
        fix="Use a safe parser.",
        test="Pass an expression as data.",
    )


class ApplicationUseCaseTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        self.store = TaskStore(self.path)

    def test_session_use_case_owns_turn_continuity(self):
        sessions = SessionUseCases(self.store, max_diff_bytes=10_000)
        turn = self.store.start_session_turn("tenant", "org/repo", 7, "sha-1", "opened")
        report = ReviewReport("org/repo", 7, "one issue", "high", [_finding()])

        note = sessions.record_review_turn(
            {
                "task_id": "task-1",
                "repository": "org/repo",
                "session_id": turn["session_id"],
                "turn_id": turn["turn_id"],
                "head_sha": "sha-1",
            },
            report,
        )

        self.assertEqual("", note)
        timeline = sessions.get_for_pull_request("org/repo", 7, "tenant")
        self.assertEqual(1, len(timeline["turns"]))
        self.assertEqual(1, timeline["turns"][0]["summary"]["new"])

    def test_model_usage_reconciliation_is_tenant_scoped_and_audited(self):
        request_id = "stale-model-request"
        self.assertTrue(
            self.store.reserve_model_usage(
                {
                    "request_id": request_id,
                    "tenant_id": "tenant",
                    "repository": "org/repo",
                    "purpose": "review",
                    "provider": "provider",
                    "model": "model",
                    "reserved_tokens": 100,
                    "reserved_cost_micros": 50,
                    "request_sha256": "a" * 64,
                    "created_at": "2000-01-01T00:00:00+00:00",
                },
                "2000-01-01T00:00:00+00:00",
            )
        )
        self.assertEqual(
            1,
            self.store.expire_model_usage_reservations("2001-01-01T00:00:00+00:00"),
        )
        use_cases = ModelUsageUseCases(self.store)

        hidden = use_cases.reconcile("other-tenant", "operator", request_id, "failed", 0, 0, 0)
        result = use_cases.reconcile("tenant", "operator", request_id, "success", 11, 7, 9)

        self.assertFalse(hidden["reconciled"])
        self.assertTrue(result["reconciled"])
        usage = self.store.list_model_usage("tenant", "org/repo")[0]
        self.assertEqual(
            ("success", 11, 7, 9),
            (
                usage["status"],
                usage["input_tokens"],
                usage["output_tokens"],
                usage["cost_micros"],
            ),
        )
        audit = self.store.list_audit("tenant")
        self.assertEqual("model-usage.reconciled", audit[0]["action"])
        self.assertEqual(request_id, audit[0]["resource"])

        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            use_cases.reconcile("tenant", "operator", "another", "failed", True, 0, 0)

    def test_model_usage_reconciliation_rolls_back_when_audit_write_fails(self):
        request_id = "stale-model-request"
        self.store.reserve_model_usage(
            {
                "request_id": request_id,
                "tenant_id": "tenant",
                "repository": "org/repo",
                "purpose": "review",
                "provider": "provider",
                "model": "model",
                "reserved_tokens": 100,
                "request_sha256": "a" * 64,
                "created_at": "2000-01-01T00:00:00+00:00",
            },
            "2000-01-01T00:00:00+00:00",
        )
        self.store.expire_model_usage_reservations("2001-01-01T00:00:00+00:00")
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TRIGGER fail_model_usage_audit BEFORE INSERT ON audit_log "
                "WHEN NEW.action='model-usage.reconciled' "
                "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit unavailable"):
            ModelUsageUseCases(self.store).reconcile(
                "tenant", "operator", request_id, "success", 11, 7, 9
            )

        usage = self.store.list_model_usage("tenant", "org/repo")[0]
        self.assertEqual("uncertain", usage["status"])
        self.assertIsNone(usage["completed_at"])

    def test_policy_use_case_rejects_rules_missing_from_runtime(self):
        policies = RepositoryPolicyResolver(self.store)
        use_cases = PolicyUseCases(
            self.store,
            policies,
            lambda: ("SEC-YAML-LOAD",),
        )
        with self.assertRaisesRegex(ValueError, "unavailable fix rules"):
            use_cases.set_repository_policy(
                "tenant",
                "org/repo",
                {"auto_fix": True, "allowed_fix_rules": ["UNKNOWN-RULE"]},
                "alice",
            )

        saved = use_cases.set_repository_policy(
            "tenant",
            "org/repo",
            {"auto_fix": True, "allowed_fix_rules": ["SEC-YAML-LOAD"]},
            "alice",
        )
        current = use_cases.get_repository_policy("tenant", "org/repo")
        self.assertEqual(saved["version"], current["version"])
        self.assertEqual(["SEC-YAML-LOAD"], current["policy"]["allowed_fix_rules"])

    def test_session_use_case_validates_impact_payload_at_boundary(self):
        sessions = SessionUseCases(self.store, max_diff_bytes=10)
        with self.assertRaisesRegex(ValueError, "maximum analysable size"):
            sessions.analyze_impact({"app.py": "x" * 101}, ["app.py"])

    def test_repair_use_case_is_idempotent_and_policy_scoped(self):
        task_id = "repair-task"
        self.store.create(task_id, "org/repo", 7, {}, "tenant")
        self.store.succeed(
            task_id,
            ReviewReport("org/repo", 7, "one issue", "high", [_finding()]),
            TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
        )
        policies = RepositoryPolicyResolver(self.store)
        policies.save(
            "tenant",
            "org/repo",
            {"auto_fix": True, "allowed_fix_rules": ["SEC-EVAL"]},
            "alice",
        )

        class Fixer:
            rule_ids = ("SEC-EVAL", "SEC-YAML-LOAD")
            calls = 0
            allowed = ()

            def create_fix_commits(self, *_args, **kwargs):
                self.calls += 1
                self.allowed = kwargs["allowed_rule_ids"]
                return {"branch": "evoagent/fix", "commits": [{"sha": "abc"}]}

        fixer = Fixer()
        events = []
        repairs = RepairUseCases(
            self.store,
            policies,
            fixer,
            lambda _installation_id: object(),
            lambda name, payload: events.append((name, payload)),
            RepairOptions(
                max_diff_bytes=10_000,
                verify_timeout_seconds=10,
                container_image="python:3.12-slim",
                memory_mb=256,
                pids_limit=64,
                cpus=1.0,
                max_output_bytes=10_000,
                effect_lease_seconds=30,
            ),
        )

        first = repairs.create_fix(task_id, tenant_id="tenant")
        second = repairs.create_fix(task_id, tenant_id="tenant")

        self.assertEqual(first, second)
        self.assertEqual(1, fixer.calls)
        self.assertEqual(("SEC-EVAL",), fixer.allowed)
        self.assertEqual("fix.completed", events[0][0])

    def test_review_use_case_owns_admission_execution_and_events(self):
        report = ReviewReport("org/repo", 7, "one issue", "high", [_finding()])
        events = []

        class Queue:
            backend = "test"

        def execute(task_id, _repository, _pull_request, _diff, _tenant_id):
            self.store.succeed(
                task_id,
                report,
                TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
            )
            return report

        reviews = ReviewUseCases(
            self.store,
            RepositoryPolicyResolver(self.store),
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: Queue(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: object(),
            lambda name, payload: events.append((name, payload)),
            ReviewOptions(
                max_diff_bytes=10_000,
                queue_lease_seconds=60,
                auto_post_review=False,
            ),
        )

        result = reviews.create_review(
            "org/repo", "--- a/a.py\n+++ b/a.py\n", 7, tenant_id="tenant"
        )

        task = self.store.get(result["task_id"], "tenant")
        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual(0, task["input"]["repository_policy"]["version"])
        self.assertTrue(task["input"]["repository_policy"]["policy"]["enabled"])
        self.assertEqual(["review.started", "review.completed"], [name for name, _ in events])

    def test_review_use_case_fetches_deferred_diff_and_deduplicates_comment(self):
        report = ReviewReport("org/repo", 7, "one issue", "high", [_finding()])

        class Queue:
            backend = "test"

        class CodeHost:
            fetches = 0
            comments = 0

            def ensure_repository_access(self, repository):
                self.repository = repository

            def fetch_diff(self, _url, max_bytes=None):
                self.fetches += 1
                self.max_bytes = max_bytes
                return "--- a/a.py\n+++ b/a.py\n"

            def upsert_comment(self, _url, _body, _marker):
                self.comments += 1

        code_host = CodeHost()
        executions = []

        def execute(task_id, _repository, _pull_request, _diff, _tenant_id):
            executions.append(task_id)
            self.store.succeed(
                task_id,
                report,
                TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
            )
            return report

        reviews = ReviewUseCases(
            self.store,
            RepositoryPolicyResolver(self.store),
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: Queue(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "continuity",
            lambda _installation_id: code_host,
            lambda *_args: None,
            ReviewOptions(10_000, 60, True),
        )
        payload = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "diff_url": "https://example.invalid/pull.diff",
            "github_issue_url": "https://api.example.invalid/issues/7",
        }
        task_id = reviews.create_deferred_task(
            "org/repo",
            7,
            "github-webhook",
            "tenant",
            {"diff_url": payload["diff_url"]},
            payload,
        )
        queued = {"task_id": task_id, **payload}

        reviews.process_queued(queued)
        reviews.process_queued(queued)

        self.assertEqual(1, code_host.fetches)
        self.assertEqual(1, code_host.comments)
        self.assertEqual([task_id], executions)
        self.assertEqual("org/repo", code_host.repository)
        self.assertEqual(14_096, code_host.max_bytes)
        task = self.store.get(task_id, "tenant")
        self.assertFalse(task["input"]["diff_pending"])
        self.assertIsNotNone(self.store.get_task_payload(task_id))

    def test_webhook_use_case_creates_one_durable_review_unit(self):
        class Queue:
            backend = "test"

        queue = Queue()
        policies = RepositoryPolicyResolver(self.store)
        reviews = ReviewUseCases(
            self.store,
            policies,
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: queue,
            lambda: None,
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: object(),
            lambda *_args: None,
            ReviewOptions(10_000, 60, True),
        )
        notifications = []
        webhooks = WebhookUseCases(
            self.store,
            reviews,
            lambda: queue,
            lambda: notifications.append("ready"),
            WebhookOptions("default", True),
        )
        payload = {
            "action": "opened",
            "number": 7,
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "diff_url": "https://example.invalid/pull.diff",
                "issue_url": "https://api.example.invalid/issues/7",
                "head": {"sha": "head-1"},
            },
        }

        first = webhooks.handle_github_pull_request(payload, "delivery-1", "payload-hash")
        duplicate = webhooks.handle_github_pull_request(payload, "delivery-1", "payload-hash")

        self.assertEqual("PENDING", first["state"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["task_id"], duplicate["task_id"])
        self.assertEqual(["ready"], notifications)
        task = self.store.get(first["task_id"], "default")
        self.assertEqual(first["session_id"], task["input"]["session_id"])
        self.assertEqual(first["task_id"], self.store.get_webhook("delivery-1")["task_id"])

        self.store.claim_webhook("legacy-unbound", "default", "pull_request", "legacy-hash")
        recovered = webhooks.handle_github_pull_request(payload, "legacy-unbound", "legacy-hash")
        self.assertNotIn("duplicate", recovered)
        self.assertEqual(recovered["task_id"], self.store.get_webhook("legacy-unbound")["task_id"])

    def test_invalid_webhook_does_not_poison_delivery_id(self):
        class StubReviews:
            pass

        class Queue:
            backend = "test"

        webhooks = WebhookUseCases(
            self.store,
            StubReviews(),
            lambda: Queue(),
            lambda: None,
            WebhookOptions("default", False),
        )
        with self.assertRaisesRegex(ValueError, "invalid GitHub"):
            webhooks.handle_github_pull_request(
                {"action": "opened", "number": 7}, "bad-delivery", "hash"
            )
        self.assertIsNone(self.store.get_webhook("bad-delivery"))


if __name__ == "__main__":
    unittest.main()
