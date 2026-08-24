import json
import threading
import time
import unittest
from unittest import mock

from evoagent.metrics import Metrics
from evoagent.task_queue import PermanentTaskError, TaskQueue


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TaskQueueBackendTests(unittest.TestCase):
    def test_resource_limits_cannot_be_disabled(self):
        for options, message in (
            ({"workers": 0}, "workers"),
            ({"workers": True}, "workers"),
            ({"workers": 257}, "workers"),
            ({"max_attempts": 0}, "attempts and lease"),
            ({"max_attempts": True}, "attempts and lease"),
            ({"lease_seconds": float("nan")}, "attempts and lease"),
            ({"backoff_base": 0}, "backoff limits"),
            ({"backoff_cap": True}, "backoff limits"),
            ({"backoff_cap": float("nan")}, "backoff limits"),
        ):
            with self.subTest(options=options):
                with self.assertRaisesRegex(ValueError, message):
                    TaskQueue(lambda _payload: None, **options)

    def test_memory_backend_is_named_and_reports_non_durable(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        try:
            self.assertEqual("memory-ephemeral", queue.backend)
            self.assertFalse(queue.durable)
            self.assertTrue(queue.health()["healthy"])
            self.assertFalse(queue.health()["lease_heartbeat_running"])
        finally:
            queue.close()

    def test_closed_memory_queue_reports_unhealthy(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        queue.close()
        self.assertFalse(queue.health()["healthy"])

    def test_memory_worker_failure_is_reported_without_error_text(self):
        def fail_dead_letter(_payload, _error):
            raise RuntimeError("database password leaked")

        queue = TaskQueue(
            lambda _payload: (_ for _ in ()).throw(PermanentTaskError("bad task")),
            workers=1,
            on_dead_letter=fail_dead_letter,
        )
        try:
            queue.submit({"task_id": "broken-callback"})
            self.assertTrue(_wait(lambda: not queue.health()["healthy"]))
            self.assertRegex(
                queue.health()["last_error"],
                r"^queue dependency failed \[type=builtins\.RuntimeError; ref=[0-9a-f]{16}\]$",
            )
            self.assertNotIn("password", queue.health()["last_error"])
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

    def test_redis_retry_uses_bounded_exponential_backoff(self):
        queue = TaskQueue(
            mock.Mock(side_effect=RuntimeError("transient")),
            workers=1,
            max_attempts=3,
            backoff_base=0.25,
            backoff_cap=0.3,
        )
        queue._redis = mock.MagicMock()
        envelope = {
            "message_id": "retry",
            "attempt": 1,
            "payload": {"task_id": "retry"},
            "submitted_at": 0.0,
        }
        try:
            with mock.patch.object(queue._stop, "wait", return_value=False) as wait:
                queue._deliver(envelope)

            wait.assert_called_once_with(0.3)
            queue._redis.xadd.assert_called_once()
            self.assertEqual(2, envelope["attempt"])
        finally:
            queue.close()

    def test_retry_cap_is_applied_before_a_huge_exponent_is_computed(self):
        queue = TaskQueue(
            mock.Mock(side_effect=RuntimeError("transient")),
            workers=1,
            max_attempts=1_000_002,
            backoff_base=0.25,
            backoff_cap=0.3,
        )
        queue._redis = mock.MagicMock()
        envelope = {
            "message_id": "retry",
            "attempt": 999_999,
            "payload": {"task_id": "retry"},
            "submitted_at": 0.0,
        }
        try:
            with mock.patch.object(queue._stop, "wait", return_value=False) as wait:
                queue._deliver(envelope)

            wait.assert_called_once_with(0.3)
            queue._redis.xadd.assert_called_once()
            self.assertEqual(1_000_000, envelope["attempt"])
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
            self.assertEqual("perm", dead[0][0]["_queue_message_id"])
        finally:
            queue.close()

    def test_memory_dead_letter_incident_buffer_is_bounded(self):
        with mock.patch.object(TaskQueue, "MAX_DLQ_MESSAGES", 2):
            queue = TaskQueue(lambda _payload: None, workers=1)
        try:
            for message_id in ("old", "middle", "new"):
                queue._dead_letter({"message_id": message_id, "payload": {}}, "failed")

            self.assertEqual(
                ["new", "middle"], [item["message_id"] for item in queue.dead_letters()]
            )
            self.assertEqual(2, queue.dead_letter_depth())
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

    def test_redis_entry_claimed_during_close_is_left_for_reclaim(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload), workers=1)
        self.assertTrue(queue.close())
        queue._redis = mock.MagicMock()

        queue._consume_redis_entry(
            "1-0",
            {
                "envelope": '{"message_id":"late","attempt":0,'
                '"payload":{"task_id":"late"},"submitted_at":0}'
            },
        )

        self.assertEqual([], seen)
        queue._redis.pipeline.assert_not_called()

    def test_duplicate_redis_claim_is_not_delivered_twice_in_one_process(self):
        started = threading.Event()
        release = threading.Event()
        seen = []

        def handler(payload):
            seen.append(payload["task_id"])
            started.set()
            release.wait(5)

        queue = TaskQueue(handler, workers=1)
        queue._redis = mock.MagicMock()
        fields = {
            "envelope": '{"message_id":"same","attempt":0,'
            '"payload":{"task_id":"same"},"submitted_at":0}'
        }
        worker = threading.Thread(target=queue._consume_redis_entry, args=("1-0", fields))
        try:
            worker.start()
            self.assertTrue(started.wait(2))

            queue._consume_redis_entry("1-0", fields)

            self.assertEqual(["same"], seen)
            self.assertIn("1-0", queue._redis_active_ids)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            queue._redis.pipeline.assert_called_once()
        finally:
            release.set()
            worker.join(2)
            queue.close()

    def test_redis_entry_with_duplicate_payload_is_dead_lettered(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload), workers=1)
        queue._redis = mock.MagicMock()
        try:
            queue._consume_redis_entry(
                "1-0",
                {
                    "envelope": '{"message_id":"ambiguous","attempt":0,'
                    '"payload":{"task_id":"first"},'
                    '"payload":{"task_id":"second"},"submitted_at":0}'
                },
            )

            self.assertEqual([], seen)
            queue._redis.xadd.assert_called_once_with(
                TaskQueue.DLQ,
                mock.ANY,
                maxlen=TaskQueue.MAX_DLQ_MESSAGES,
                approximate=True,
            )
            queue._redis.pipeline.assert_called_once()
        finally:
            queue.close()

    def test_redis_entries_with_invalid_envelopes_are_dead_lettered_once(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload), workers=1)
        queue._redis = mock.MagicMock()
        invalid = (
            {"message_id": "bad", "attempt": "0", "payload": {}, "submitted_at": 0},
            {"message_id": "bad", "attempt": True, "payload": {}, "submitted_at": 0},
            {"message_id": "bad", "attempt": -1, "payload": {}, "submitted_at": 0},
            {"message_id": "bad", "attempt": 3, "payload": {}, "submitted_at": 0},
            {"message_id": "bad", "attempt": 0, "payload": [], "submitted_at": 0},
            {"message_id": "", "attempt": 0, "payload": {}, "submitted_at": 0},
            {"message_id": "bad", "attempt": 0, "payload": {}, "submitted_at": "0"},
        )
        try:
            for index, envelope in enumerate(invalid):
                with self.subTest(envelope=envelope):
                    queue._consume_redis_entry(
                        "%d-0" % index,
                        {"envelope": json.dumps(envelope)},
                    )

            self.assertEqual([], seen)
            self.assertEqual(len(invalid), queue._redis.xadd.call_count)
            self.assertEqual(len(invalid), queue._redis.pipeline.call_count)
        finally:
            queue.close()

    def test_queue_envelope_byte_limit_applies_to_publish_and_consume(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload), workers=1)
        queue._redis = mock.MagicMock()
        try:
            with mock.patch.object(TaskQueue, "MAX_ENVELOPE_BYTES", 100):
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    queue.submit({"task_id": "large", "value": "x" * 100})
                queue._consume_redis_entry("1-0", {"envelope": "x" * 101})

            self.assertEqual([], seen)
            queue._redis.xadd.assert_called_once()
            queue._redis.pipeline.assert_called_once()
        finally:
            queue.close()

    def test_corrupt_dead_letter_does_not_hide_valid_messages(self):
        queue = TaskQueue(lambda _payload: None, workers=1)
        queue._redis = mock.MagicMock()
        valid = {
            "message_id": "valid",
            "payload": {"task_id": "valid"},
            "error": "task delivery failed [type=unknown; ref=0000000000000000]",
        }
        queue._redis.xrevrange.return_value = [
            ("3-0", {"envelope": "{"}),
            ("2-0", {"envelope": json.dumps(valid)}),
            ("1-0", {"envelope": "[]"}),
        ]
        captured = Metrics()
        try:
            with mock.patch("evoagent.task_queue.metrics", captured):
                self.assertEqual([valid], queue.dead_letters())

            self.assertIn(
                "evoagent_queue_dead_letter_decode_failures_total 2.0",
                captured.prometheus(),
            )
        finally:
            queue.close()

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

    def test_submit_rejects_json_that_cannot_round_trip_through_redis(self):
        seen = []
        queue = TaskQueue(lambda payload: seen.append(payload), workers=1)
        try:
            for payload in (
                [],
                {"task_id": "nan", "value": float("nan")},
                {"task_id": "surrogate", "value": "\ud800"},
                {"task_id": "keys", "value": {1: "numeric"}},
            ):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    queue.submit(payload)
            self.assertEqual([], seen)
        finally:
            queue.close()


if __name__ == "__main__":
    unittest.main()
