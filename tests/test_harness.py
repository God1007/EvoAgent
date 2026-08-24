import unittest
from unittest import mock

from evoagent.harness import BudgetExceeded, ReviewHarness, TaskCancelled
from evoagent.metrics import Metrics
from evoagent.reviewer import LocalRuleReviewer
from tests.db_support import postgres_store


class CancellationRaceTests(unittest.TestCase):
    def test_execution_budgets_cannot_be_disabled(self):
        for options, message in (
            ({"max_steps": 0}, "max_steps"),
            ({"max_steps": True}, "max_steps"),
            ({"max_steps": float("nan")}, "max_steps"),
            ({"timeout_seconds": 0}, "timeout"),
            ({"timeout_seconds": True}, "timeout"),
            ({"timeout_seconds": float("nan")}, "timeout"),
            ({"timeout_seconds": float("inf")}, "timeout"),
            ({"node_retries": -1}, "node_retries"),
            ({"node_retries": True}, "node_retries"),
            ({"node_retries": float("nan")}, "node_retries"),
        ):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, message):
                    ReviewHarness(mock.Mock(), LocalRuleReviewer(), **options)

    def test_failure_case_write_failure_is_visible_without_masking_review_error(self):
        store = mock.Mock()
        store.get.return_value = {"state": "PENDING", "trace": []}
        store.load_checkpoints.return_value = {}
        store.is_cancelled.return_value = False
        store.fail.return_value = True
        store.record_failure_case.side_effect = RuntimeError("database unavailable")
        captured = Metrics()

        with (
            mock.patch("evoagent.harness.metrics", captured),
            self.assertRaisesRegex(ValueError, "valid unified diff"),
        ):
            ReviewHarness(store, LocalRuleReviewer()).run("task", "demo/repo", 7, "invalid")

        self.assertIn(
            "evoagent_failure_case_persistence_failures_total 1.0",
            captured.prometheus(),
        )

    def test_cancelled_task_cannot_commit_success(self):
        store = mock.Mock()
        store.get.return_value = {"state": "PENDING", "trace": []}
        store.load_checkpoints.return_value = {}
        store.is_cancelled.return_value = False
        store.transition.return_value = True
        store.succeed.return_value = False
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+eval(value)\n"

        with self.assertRaises(TaskCancelled):
            ReviewHarness(store, LocalRuleReviewer()).run("task", "demo/repo", 7, diff, 2)

        self.assertEqual(2, store.succeed.call_args.args[-1])
        self.assertTrue(
            all(call.kwargs["generation"] == 2 for call in store.save_checkpoint.call_args_list)
        )
        self.assertTrue(all(call.args[-1] == 2 for call in store.transition.call_args_list))
        store.cancel.assert_called_once()
        self.assertEqual(2, store.cancel.call_args.args[-1])
        store.record_failure_case.assert_not_called()

    def test_late_node_result_is_never_checkpointed(self):
        store = mock.Mock()
        store.load_checkpoints.return_value = {}
        store.is_cancelled.return_value = False
        harness = ReviewHarness(store, LocalRuleReviewer(), timeout_seconds=1)
        harness._ctx.started = 0.0
        harness._ctx.step = 0
        harness._ctx.task_id = "task"

        with (
            mock.patch("evoagent.harness.time.monotonic", side_effect=[0.0, 2.0]),
            self.assertRaises(BudgetExceeded),
        ):
            harness._run_node("task", "executing", lambda: {"findings": ["late"]})

        store.save_checkpoint.assert_not_called()

    def test_atomic_checkpoint_rejection_becomes_task_cancellation(self):
        store = mock.Mock()
        store.load_checkpoints.return_value = {}
        store.is_cancelled.return_value = False
        store.save_checkpoint.return_value = False
        harness = ReviewHarness(store, LocalRuleReviewer(), timeout_seconds=10**12)
        harness._ctx.started = 0.0
        harness._ctx.step = 0
        harness._ctx.task_id = "task"

        with self.assertRaises(TaskCancelled):
            harness._run_node("task", "executing", lambda: {"findings": []})

    def test_losing_node_reuses_concurrently_completed_checkpoint(self):
        winner = {"findings": ["winner"]}
        for error, failed_writes in ((RuntimeError("lost"), 1), (ValueError("lost"), 0)):
            with self.subTest(error=type(error).__name__):
                store = mock.Mock()
                store.load_checkpoints.side_effect = [
                    {},
                    {"executing": {"status": "completed", "state": winner}},
                ]
                store.is_cancelled.return_value = False
                store.save_checkpoint.return_value = True
                callback = mock.Mock(side_effect=error)
                harness = ReviewHarness(
                    store,
                    LocalRuleReviewer(),
                    timeout_seconds=10**12,
                    node_retries=2,
                )
                harness._ctx.started = 0.0
                harness._ctx.step = 0
                harness._ctx.task_id = "task"

                self.assertEqual(winner, harness._run_node("task", "executing", callback))
                callback.assert_called_once()
                self.assertEqual(failed_writes, store.save_checkpoint.call_count)

    def test_successful_node_uses_first_durable_checkpoint(self):
        local = {"findings": ["local"]}
        winner = {"findings": ["winner"]}
        store = mock.Mock()
        store.load_checkpoints.side_effect = [
            {},
            {"executing": {"status": "completed", "state": winner}},
        ]
        store.is_cancelled.return_value = False
        store.save_checkpoint.return_value = True
        harness = ReviewHarness(store, LocalRuleReviewer(), timeout_seconds=10**12)
        harness._ctx.started = 0.0
        harness._ctx.step = 0
        harness._ctx.task_id = "task"

        self.assertEqual(winner, harness._run_node("task", "executing", lambda: local))
        store.save_checkpoint.assert_called_once()


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.store = postgres_store(self)

    def test_successful_state_flow_is_persisted(self):
        task_id = "test-task"
        self.store.create(task_id, "demo/repo", 7, {"source": "test"})
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+eval(value)\n"
        report = ReviewHarness(self.store, LocalRuleReviewer()).run(task_id, "demo/repo", 7, diff)
        task = self.store.get(task_id)
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual(
            ["PLANNING", "EXECUTING", "REVIEWING", "SUCCESS"], [x["state"] for x in task["trace"]]
        )
        self.assertEqual("high", report.risk)

    def test_invalid_diff_is_recorded_as_failure(self):
        task_id = "bad-task"
        self.store.create(task_id, "demo/repo", None, {"source": "test"})
        with self.assertRaises(ValueError):
            ReviewHarness(self.store, LocalRuleReviewer()).run(
                task_id, "demo/repo", None, "not a diff"
            )
        self.assertEqual("FAILED", self.store.get(task_id)["state"])


if __name__ == "__main__":
    unittest.main()
