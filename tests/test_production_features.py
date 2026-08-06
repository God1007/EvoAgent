import os
import tempfile
import time
import unittest

from evoagent.auth import AuthManager
from evoagent.harness import ReviewHarness
from evoagent.reviewer import LocalRuleReviewer
from evoagent.rollout import ReleaseManager
from evoagent.service import ReviewService
from evoagent.store import TaskStore
from evoagent.task_queue import TaskQueue
from evoagent.verifier import RepairVerifier


DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"


class ProductionFeatureTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_login_rbac_and_tenant_task_isolation(self):
        auth = AuthManager(
            self.store, "a" * 32, bootstrap_username="alice",
            bootstrap_password="correct-horse", default_tenant_id="tenant-a",
        )
        token = auth.login("alice", "correct-horse")["access_token"]
        principal = auth.authenticate("Bearer " + token)
        self.assertTrue(principal.can("manage"))
        self.store.create("a", "org/a", 1, {}, "tenant-a")
        self.store.create("b", "org/b", 2, {}, "tenant-b")
        self.assertIsNotNone(self.store.get("a", principal.tenant_id))
        self.assertIsNone(self.store.get("b", principal.tenant_id))
        self.assertEqual(["a"], [item["id"] for item in self.store.list_tasks(10, "tenant-a")])

    def test_webhook_delivery_is_idempotent_and_payload_bound(self):
        self.assertTrue(self.store.claim_webhook("delivery-1", "t", "pull_request", "aaa"))
        self.assertFalse(self.store.claim_webhook("delivery-1", "t", "pull_request", "aaa"))
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.store.claim_webhook("delivery-1", "t", "pull_request", "bbb")

    def test_failure_cases_are_filtered_by_tenant(self):
        self.store.create("a", "org/a", 1, {}, "tenant-a")
        self.store.create("b", "org/b", 2, {}, "tenant-b")
        self.store.record_failure_case("a", "false_positive", {"note": "a"})
        self.store.record_failure_case("b", "missed_issue", {"note": "b"})

        cases = self.store.list_failure_cases(tenant_id="tenant-a")

        self.assertEqual(["a"], [item["task_id"] for item in cases])

    def test_failed_graph_resumes_after_last_completed_checkpoint(self):
        class BrokenReviewer:
            name = "broken"

            def review(self, _diff, _parsed):
                raise RuntimeError("temporary provider failure")

        self.store.create("task", "org/repo", 1, {})
        with self.assertRaises(RuntimeError):
            ReviewHarness(
                self.store, BrokenReviewer(), node_retries=0
            ).run("task", "org/repo", 1, DIFF)
        checkpoints = self.store.load_checkpoints("task")
        self.assertEqual("completed", checkpoints["planning"]["status"])
        self.assertEqual("failed", checkpoints["executing"]["status"])

        report = ReviewHarness(
            self.store, LocalRuleReviewer(), node_retries=0
        ).resume("task", "org/repo", 1, DIFF)
        self.assertEqual("high", report.risk)
        planning_events = [
            item for item in self.store.get("task")["trace"] if item["state"] == "PLANNING"
        ]
        self.assertEqual(1, len(planning_events))

    def test_queue_moves_terminal_failure_to_dlq(self):
        def broken(_payload):
            raise RuntimeError("boom")

        queue = TaskQueue(broken, workers=1, max_attempts=1)
        queue.submit({"task_id": "dead"})
        for _ in range(100):
            if queue.dead_letters():
                break
            time.sleep(.01)
        letters = queue.dead_letters()
        queue.close()
        self.assertEqual("dead", letters[0]["message_id"])
        self.assertIn("boom", letters[0]["error"])

    def test_dead_letter_marks_pending_task_failed(self):
        self.store.create("dead", "org/repo", 1, {}, "tenant")
        service = ReviewService.__new__(ReviewService)
        service.store = self.store

        service._on_dead_letter({"task_id": "dead", "tenant_id": "tenant"}, "boom")

        task = self.store.get("dead", "tenant")
        self.assertEqual("FAILED", task["state"])
        self.assertEqual("boom", task["error"])

    def test_canary_assignment_and_error_budget_rollback(self):
        release = ReleaseManager(self.store)
        release.configure("tenant", "skill", {
            "stable_version": 1, "candidate_version": 2,
            "canary_percent": 100, "shadow_percent": 100,
            "min_samples": 2, "max_error_rate": .25,
        })
        self.assertEqual("canary", release.assignment("tenant", "skill", "task")["lane"])
        release.observe("tenant", "skill", True)
        result = release.observe("tenant", "skill", False)
        self.assertEqual("rolled_back", result["status"])
        self.assertTrue(self.store.list_alerts("tenant"))

    def test_repair_verifier_blocks_invalid_python(self):
        result = RepairVerifier().verify_contents({"app.py": "def broken(:\n"})
        self.assertFalse(result["passed"])
        self.assertEqual("compile:app.py", result["checks"][0]["name"])


if __name__ == "__main__":
    unittest.main()
