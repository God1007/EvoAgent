import tempfile
import threading
import time
import unittest

from evoagent.task_queue import PermanentTaskError, TaskQueue, load_tenant_fair_policy


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TaskQueueBackendTests(unittest.TestCase):
    def test_tenant_fair_policy_is_bounded_and_content_addressed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as handle:
            handle.write(
                'version = 1\nid = "priority-v1"\ndefault_weight = 2\n[tenants]\n"tenant-a" = 4\n'
            )
            handle.flush()
            policy = load_tenant_fair_policy(handle.name)

        self.assertEqual(4, policy.weight("tenant-a"))
        self.assertEqual(2, policy.weight("tenant-b"))
        self.assertEqual("priority-v1", policy.policy_id)
        self.assertRegex(policy.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(load_tenant_fair_policy().sha256, load_tenant_fair_policy().sha256)

    def test_tenant_fair_policy_rejects_unsafe_or_invalid_documents(self):
        invalid = (
            "version = 2\n",
            "version = 1\n",
            'version = 1\nid = "valid-v1"\ndefault_weight = 0\n',
            'version = 1\nid = "valid-v1"\nunknown = true\n',
            'version = 1\nid = "bad id"\n',
            'version = 1\nid = "valid-v1"\n[tenants]\n" tenant" = 2\n',
            'version = 1\nid = "valid-v1"\n[tenants]\n"tenant" = true\n',
        )
        for document in invalid:
            with self.subTest(document=document):
                with tempfile.NamedTemporaryFile("w", suffix=".toml") as handle:
                    handle.write(document)
                    handle.flush()
                    with self.assertRaises(ValueError):
                        load_tenant_fair_policy(handle.name)

    def test_fair_scheduling_requires_redis(self):
        with self.assertRaisesRegex(ValueError, "requires EVOAGENT_REDIS_URL"):
            TaskQueue(lambda _payload: None, workers=1, fair_scheduling=True)

    def test_memory_backend_is_named_and_reports_non_durable(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        try:
            self.assertEqual("memory-ephemeral", queue.backend)
            self.assertFalse(queue.durable)
            self.assertFalse(queue.fair_scheduling)
            self.assertEqual(1, queue.tenant_weight("tenant"))
            self.assertEqual("uniform-v1", queue.fair_policy_id)
            self.assertEqual(0, queue.fair_waiting_tenants())
            self.assertTrue(queue.health()["healthy"])
            self.assertFalse(queue.health()["lease_heartbeat_running"])
        finally:
            queue.close()

    def test_closed_memory_queue_reports_unhealthy(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        queue.close()
        self.assertFalse(queue.health()["healthy"])

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
            error = queue.dead_letters()[0]["error"]
            self.assertRegex(
                error,
                r"^task delivery failed \[type=evoagent\.task_queue\.PermanentTaskError; "
                r"ref=[0-9a-f]{16}\]$",
            )
            self.assertNotIn("do not retry", error)
            self.assertEqual(error, dead[0][1])
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

    def test_depth_counts_scheduled_and_active_memory_deliveries(self):
        started = threading.Event()
        release = threading.Event()

        def handler(_payload):
            started.set()
            release.wait(5)

        queue = TaskQueue(handler, workers=1)
        try:
            queue.submit({"task_id": "active"})
            self.assertTrue(started.wait(2))
            self.assertEqual(1, queue.depth())
            release.set()
            self.assertTrue(queue.drain(2))
            self.assertEqual(0, queue.depth())
            self.assertEqual(0.0, queue.oldest_age_seconds())
        finally:
            release.set()
            queue.close(2)

    def test_oldest_age_and_dead_letter_depth_track_current_backlog(self):
        started = threading.Event()
        release = threading.Event()

        def slow_handler(_payload):
            started.set()
            release.wait(5)

        queue = TaskQueue(slow_handler, workers=1)
        try:
            queue.submit({"task_id": "aged"})
            self.assertTrue(started.wait(2))
            time.sleep(0.01)
            self.assertGreater(queue.oldest_age_seconds(), 0.0)
            self.assertEqual(0, queue.dead_letter_depth())
            release.set()
            self.assertTrue(queue.drain(2))
            self.assertEqual(0.0, queue.oldest_age_seconds())
        finally:
            release.set()
            queue.close(2)

        failed = TaskQueue(
            lambda _payload: (_ for _ in ()).throw(PermanentTaskError("bad")),
            workers=1,
        )
        try:
            failed.submit({"task_id": "dead"})
            self.assertTrue(_wait(lambda: failed.dead_letter_depth() == 1))
            self.assertEqual(0.0, failed.oldest_age_seconds())
        finally:
            failed.close(2)

    def test_close_waits_for_scheduled_work_before_returning(self):
        started = threading.Event()
        release = threading.Event()
        seen = []

        def handler(payload):
            seen.append(payload["task_id"])
            if payload["task_id"] == "first":
                started.set()
                release.wait(5)

        queue = TaskQueue(handler, workers=1)
        queue.submit({"task_id": "first"})
        queue.submit({"task_id": "second"})
        self.assertTrue(started.wait(2))
        result = []
        closer = threading.Thread(target=lambda: result.append(queue.close(2)))
        closer.start()
        time.sleep(0.05)
        self.assertTrue(closer.is_alive())
        release.set()
        closer.join(3)

        self.assertEqual([True], result)
        self.assertEqual(["first", "second"], seen)

    def test_close_reports_bounded_drain_timeout(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def handler(_payload):
            started.set()
            release.wait(5)
            finished.set()

        queue = TaskQueue(handler, workers=1)
        queue.submit({"task_id": "slow"})
        self.assertTrue(started.wait(2))
        self.assertFalse(queue.close(0.01))
        release.set()
        self.assertTrue(finished.wait(2))

    def test_submit_after_close_fails_fast(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        self.assertTrue(queue.close())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            queue.submit({"task_id": "late"})

    def test_duplicate_message_id_is_published_once(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload["value"]), workers=1)
        try:
            self.assertEqual("same", queue.submit({"value": 1}, message_id="same"))
            self.assertEqual("same", queue.submit({"value": 2}, message_id="same"))
            self.assertTrue(_wait(lambda: seen == [1]))
            time.sleep(0.05)
            self.assertEqual([1], seen)
        finally:
            queue.close()


if __name__ == "__main__":
    unittest.main()
