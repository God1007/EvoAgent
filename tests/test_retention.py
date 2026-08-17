import unittest
from datetime import UTC, datetime
from unittest import mock

from evoagent.metrics import Metrics
from evoagent.retention import RetentionManager, RetentionOptions


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
    def test_disabled_manager_never_touches_store(self):
        store = FakeStore()
        manager = RetentionManager(store, RetentionOptions(), autostart=False)

        self.assertEqual(
            {"trace_events": 0, "session_turns": 0, "session_findings": 0},
            manager.run_once(),
        )
        self.assertEqual([], store.calls)
        self.assertFalse(manager.status()["enabled"])
        self.assertTrue(manager.close())

    def test_run_is_bounded_aggregated_and_observable(self):
        store = FakeStore(
            [
                {"trace_events": 2, "session_turns": 2, "session_findings": 5},
                {"trace_events": 1, "session_turns": 0, "session_findings": 0},
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
            {"trace_events": 3, "session_turns": 2, "session_findings": 5},
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
