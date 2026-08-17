"""Real PostgreSQL and Redis behavior tests enabled by the CI service matrix."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid

from evoagent.postgres_store import PostgresTaskStore
from evoagent.proof_remote import RedisProofReplayStore
from evoagent.task_queue import PermanentTaskError, TaskQueue

POSTGRES_URL = os.getenv("EVOAGENT_TEST_POSTGRES_URL", "")
REDIS_URL = os.getenv("EVOAGENT_TEST_REDIS_URL", "")


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@unittest.skipUnless(POSTGRES_URL, "EVOAGENT_TEST_POSTGRES_URL is not configured")
class PostgreSQLRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = PostgresTaskStore(POSTGRES_URL, pool_min=1, pool_max=1, pool_timeout=0.2)

    def tearDown(self):
        self.store.close()

    def test_real_pool_exhaustion_is_bounded_and_recovers(self):
        from psycopg_pool import PoolTimeout

        self.assertTrue(self.store.has_pool())
        acquired = threading.Event()
        release = threading.Event()

        def hold_only_connection():
            with self.store._connect() as conn:
                conn.execute("SELECT 1")
                acquired.set()
                release.wait(5)

        holder = threading.Thread(target=hold_only_connection)
        holder.start()
        self.assertTrue(acquired.wait(2))
        started = time.monotonic()
        try:
            with self.assertRaises(PoolTimeout):
                self.store.ping()
            self.assertLess(time.monotonic() - started, 2)
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())
        self.store.ping()

    def test_pool_replaces_a_server_terminated_connection(self):
        import psycopg

        with self.store._connect() as conn:
            backend_pid = int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        with psycopg.connect(POSTGRES_URL, autocommit=True) as killer:
            terminated = killer.execute(
                "SELECT pg_terminate_backend(%s) AS terminated", (backend_pid,)
            ).fetchone()[0]
        self.assertTrue(terminated)

        failures = []

        def reconnected() -> bool:
            try:
                self.store.ping()
                return True
            except Exception as exc:  # the first checkout may discover the dead socket
                failures.append(type(exc).__name__)
                return False

        self.assertTrue(_wait(reconnected, 10), failures)


@unittest.skipUnless(REDIS_URL, "EVOAGENT_TEST_REDIS_URL is not configured")
class RedisRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        import redis

        self.redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        self.redis.delete(TaskQueue.STREAM, TaskQueue.DLQ)
        dedupe_keys = list(self.redis.scan_iter(TaskQueue.DEDUP + "*", count=1000))
        if dedupe_keys:
            self.redis.delete(*dedupe_keys)

    def tearDown(self):
        self.redis.delete(TaskQueue.STREAM, TaskQueue.DLQ)
        dedupe_keys = list(self.redis.scan_iter(TaskQueue.DEDUP + "*", count=1000))
        if dedupe_keys:
            self.redis.delete(*dedupe_keys)
        self.redis.close()

    def test_submission_dedupe_survives_queue_process_restart(self):
        message_id = "dedupe-" + uuid.uuid4().hex
        first_delivery = threading.Event()
        first = TaskQueue(lambda _payload: first_delivery.set(), workers=1, redis_url=REDIS_URL)
        try:
            first.submit({"value": 1}, message_id=message_id)
            self.assertTrue(first_delivery.wait(5))
            self.assertTrue(_wait(lambda: first.depth() == 0))
        finally:
            first.close(3)

        duplicate_delivery = threading.Event()
        restarted = TaskQueue(
            lambda _payload: duplicate_delivery.set(), workers=1, redis_url=REDIS_URL
        )
        try:
            restarted.submit({"value": 2}, message_id=message_id)
            self.assertFalse(duplicate_delivery.wait(1))
            self.assertEqual(0, restarted.depth())
        finally:
            restarted.close(3)

    def test_expired_pending_entry_is_reclaimed_after_consumer_process_dies(self):

        self.redis.xgroup_create(TaskQueue.STREAM, TaskQueue.GROUP, id="0", mkstream=True)
        message_id = "crash-" + uuid.uuid4().hex
        envelope = json.dumps(
            {
                "message_id": message_id,
                "attempt": 0,
                "payload": {"message_id": message_id},
                "submitted_at": time.time(),
            }
        )
        self.redis.xadd(TaskQueue.STREAM, {"envelope": envelope})
        script = (
            "import redis,sys; r=redis.Redis.from_url(sys.argv[1],decode_responses=True); "
            "rows=r.xreadgroup(sys.argv[2],'crashed-process',{sys.argv[3]:'>'},count=1); "
            "assert rows; r.close()"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", script, REDIS_URL, TaskQueue.GROUP, TaskQueue.STREAM],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, crashed.returncode, crashed.stderr)
        self.assertEqual(1, self.redis.xpending(TaskQueue.STREAM, TaskQueue.GROUP)["pending"])
        time.sleep(1.1)

        delivered = threading.Event()
        queue = TaskQueue(
            lambda payload: delivered.set() if payload["message_id"] == message_id else None,
            workers=1,
            redis_url=REDIS_URL,
            lease_seconds=1,
        )
        try:
            self.assertTrue(delivered.wait(5))
            self.assertTrue(_wait(lambda: queue.depth() == 0))
        finally:
            queue.close(3)

    def test_dlq_replay_survives_queue_restart(self):
        message_id = "dlq-" + uuid.uuid4().hex

        def reject(_payload):
            raise PermanentTaskError("fixture rejected")

        failed = TaskQueue(reject, workers=1, redis_url=REDIS_URL)
        try:
            failed.submit({"message_id": message_id}, message_id=message_id)
            self.assertTrue(_wait(lambda: len(failed.dead_letters()) == 1))
        finally:
            failed.close(3)

        delivered = threading.Event()
        restarted = TaskQueue(lambda _payload: delivered.set(), workers=1, redis_url=REDIS_URL)
        try:
            self.assertTrue(restarted.replay_dead_letter(message_id))
            self.assertTrue(delivered.wait(5))
        finally:
            restarted.close(3)

    def test_worker_recovers_after_its_redis_socket_is_disconnected(self):
        delivered = threading.Event()
        queue = TaskQueue(lambda _payload: delivered.set(), workers=1, redis_url=REDIS_URL)
        try:
            queue._redis.connection_pool.disconnect()
            queue.submit({"value": 1}, message_id="reconnect-" + uuid.uuid4().hex)
            self.assertTrue(delivered.wait(10), queue.health())
            self.assertTrue(queue.health()["healthy"])
        finally:
            queue.close(3)

    def test_proof_replay_claim_is_atomic_across_runner_adapters(self):
        prefix = "evoagent:test-proof-replay:%s" % uuid.uuid4().hex
        first = RedisProofReplayStore(REDIS_URL, prefix=prefix)
        second = RedisProofReplayStore(REDIS_URL, prefix=prefix)
        request_id = str(uuid.uuid4())
        try:
            self.assertTrue(first.claim(request_id, int(time.time()) + 30))
            self.assertFalse(second.claim(request_id, int(time.time()) + 30))
            self.assertTrue(first.health())
            keys = list(self.redis.scan_iter(prefix + ":*"))
            self.assertEqual(1, len(keys))
            self.assertGreater(self.redis.ttl(keys[0]), 0)
        finally:
            keys = list(self.redis.scan_iter(prefix + ":*"))
            if keys:
                self.redis.delete(*keys)
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
