import unittest
from datetime import UTC, datetime
from unittest import mock

from evoagent.metrics import Metrics
from evoagent.retention import RetentionManager, RetentionOptions

EMPTY_COUNTS = {
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
}


class FakeStore:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    def prune_operational_history(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else {}


class RetentionManagerTests(unittest.TestCase):
    def test_resource_limits_cannot_be_silently_clamped(self):
        for options, autostart, message in (
            (RetentionOptions(retention_days=-1), False, "retention days"),
            (RetentionOptions(retention_days=True), False, "limits must be integers"),
            (RetentionOptions(retention_days=36_501), False, "retention days"),
            (RetentionOptions(webhook_replay_seconds=0), False, "webhook replay window"),
            (
                RetentionOptions(retention_days=1, webhook_replay_seconds=86_400),
                False,
                "retention age",
            ),
            (RetentionOptions(interval_seconds=0), False, "retention interval"),
            (RetentionOptions(retention_days=1, interval_seconds=59), False, "interval"),
            (RetentionOptions(batch_size=0), False, "batch size"),
            (RetentionOptions(batch_size=10_001), False, "batch size"),
            (RetentionOptions(max_batches_per_run=0), False, "batches per run"),
            (RetentionOptions(), "false", "autostart"),
        ):
            with self.subTest(options=options, autostart=autostart):
                with self.assertRaisesRegex(ValueError, message):
                    RetentionManager(FakeStore(), options, autostart=autostart)

    def test_disabled_manager_never_touches_store(self):
        store = FakeStore()
        manager = RetentionManager(store, RetentionOptions(), autostart=False)

        self.assertEqual(EMPTY_COUNTS, manager.run_once())
        self.assertEqual([], store.calls)
        self.assertFalse(manager.status()["enabled"])
        self.assertTrue(manager.close())

    def test_run_is_bounded_aggregated_and_observable(self):
        store = FakeStore(
            [
                {
                    "trace_events": 2,
                    "execution_tasks": 2,
                    "task_payloads": 2,
                    "checkpoints": 4,
                    "agent_messages": 3,
                    "outbox_messages": 2,
                    "effect_receipts": 2,
                    "webhook_deliveries": 2,
                    "release_observations": 2,
                    "session_turns": 2,
                    "session_findings": 5,
                },
                {
                    "trace_events": 1,
                    "effect_receipts": 1,
                    "webhook_deliveries": 1,
                    "release_observations": 1,
                    "session_turns": 0,
                    "session_findings": 0,
                },
            ]
        )
        manager = RetentionManager(
            store,
            RetentionOptions(retention_days=30, batch_size=2),
            autostart=False,
        )
        captured = Metrics()

        with mock.patch("evoagent.retention.metrics", captured):
            result = manager.run_once(datetime(2030, 1, 31, tzinfo=UTC))

        self.assertEqual(
            {
                **EMPTY_COUNTS,
                "trace_events": 3,
                "execution_tasks": 2,
                "task_payloads": 2,
                "checkpoints": 4,
                "agent_messages": 3,
                "outbox_messages": 2,
                "effect_receipts": 3,
                "webhook_deliveries": 3,
                "release_observations": 3,
                "session_turns": 2,
                "session_findings": 5,
            },
            result,
        )
        self.assertEqual(2, len(store.calls))
        self.assertEqual("2030-01-01T00:00:00+00:00", store.calls[0][0])
        self.assertEqual(2, store.calls[0][2])
        status = manager.status()
        self.assertEqual(
            datetime(2030, 1, 31, tzinfo=UTC).timestamp(), status["last_success_timestamp"]
        )
        self.assertEqual(result, status["last_pruned"])
        output = captured.prometheus()
        self.assertIn("evoagent_retention_runs_total 1.0", output)
        self.assertIn("evoagent_retention_trace_events_pruned_total 3.0", output)
        self.assertIn("evoagent_retention_checkpoints_pruned_total 4.0", output)
        self.assertIn("evoagent_retention_outbox_messages_pruned_total 2.0", output)
        self.assertIn("evoagent_retention_effect_receipts_pruned_total 3.0", output)
        self.assertIn("evoagent_retention_webhook_deliveries_pruned_total 3.0", output)
        self.assertIn("evoagent_retention_release_observations_pruned_total 3.0", output)
        self.assertIn("evoagent_retention_session_findings_pruned_total 5.0", output)

    def test_background_boundary_records_only_failure_type(self):
        store = FakeStore(error=RuntimeError("password=must-not-leak"))
        manager = RetentionManager(
            store,
            RetentionOptions(retention_days=30),
            autostart=False,
        )
        captured = Metrics()

        with mock.patch("evoagent.retention.metrics", captured):
            self.assertFalse(manager._run_safely_once())

        status = manager.status()
        self.assertEqual("builtins.RuntimeError", status["last_error_type"])
        self.assertNotIn("must-not-leak", str(status))
        self.assertIn("evoagent_retention_failures_total 1.0", captured.prometheus())


if __name__ == "__main__":
    unittest.main()
