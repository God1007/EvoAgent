import threading
import time
import unittest

from evoagent.task_queue import PermanentTaskError, TaskQueue


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TaskQueueBackendTests(unittest.TestCase):
    def test_memory_backend_is_named_and_reports_non_durable(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        try:
            self.assertEqual("memory-ephemeral", queue.backend)
            self.assertFalse(queue.durable)
        finally:
            queue.close()

    def test_successful_delivery_invokes_handler(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload["task_id"]), workers=1)
        try:
            queue.submit({"task_id": "ok"})
            self.assertTrue(_wait(lambda: seen == ["ok"]))
            self.assertEqual([], queue.dead_letters())
        finally:
            queue.close()

    def test_transient_failure_is_retried_then_succeeds(self):
        calls = []
        done = threading.Event()

        def handler(payload):
            calls.append(payload["task_id"])
            if len(calls) < 2:
                raise RuntimeError("transient")
            done.set()

        queue = TaskQueue(handler, workers=1, max_attempts=3, backoff_base=0.01)
        try:
            queue.submit({"task_id": "retry"})
            self.assertTrue(done.wait(5))
            self.assertTrue(_wait(lambda: len(calls) == 2))
            self.assertEqual([], queue.dead_letters())
        finally:
            queue.close()

    def test_permanent_error_is_dead_lettered_without_retry(self):
        calls = []

        def handler(_payload):
            calls.append(1)
            raise PermanentTaskError("do not retry")

        dead = []
        queue = TaskQueue(
            handler,
            workers=1,
            max_attempts=5,
            on_dead_letter=lambda payload, error: dead.append((payload, error)),
        )
        try:
            queue.submit({"task_id": "perm"})
            self.assertTrue(_wait(lambda: queue.dead_letters()))
            time.sleep(0.2)
            self.assertEqual(1, len(calls))
            self.assertEqual(1, len(dead))
            self.assertIn("do not retry", queue.dead_letters()[0]["error"])
        finally:
            queue.close()

    def test_replay_dead_letter_redelivers_and_succeeds(self):
        state = {"fail": True}
        succeeded = threading.Event()

        def handler(_payload):
            if state["fail"]:
                raise RuntimeError("boom")
            succeeded.set()

        queue = TaskQueue(handler, workers=1, max_attempts=1)
        try:
            queue.submit({"task_id": "replayable"})
            self.assertTrue(_wait(lambda: queue.dead_letters()))
            state["fail"] = False
            self.assertTrue(queue.replay_dead_letter("replayable"))
            self.assertTrue(succeeded.wait(5))
        finally:
            queue.close()


if __name__ == "__main__":
    unittest.main()
