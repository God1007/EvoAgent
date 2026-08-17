"""Real persistence and object-store tests enabled by explicit environments."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from evoagent.postgres_store import PostgresTaskStore
from evoagent.proof_artifacts import S3ObjectLockArtifactStore
from evoagent.proof_remote import RedisProofReplayStore
from evoagent.task_queue import PermanentTaskError, TaskQueue, load_tenant_fair_policy

POSTGRES_URL = os.getenv("EVOAGENT_TEST_POSTGRES_URL", "")
REDIS_URL = os.getenv("EVOAGENT_TEST_REDIS_URL", "")
S3_OBJECT_LOCK_BUCKET = os.getenv("EVOAGENT_TEST_S3_OBJECT_LOCK_BUCKET", "")
S3_REGION = os.getenv("EVOAGENT_TEST_S3_REGION", "")


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
        self._clear_queue_state()
        dedupe_keys = list(self.redis.scan_iter(TaskQueue.DEDUP + "*", count=1000))
        if dedupe_keys:
            self.redis.delete(*dedupe_keys)

    def tearDown(self):
        self._clear_queue_state()
        dedupe_keys = list(self.redis.scan_iter(TaskQueue.DEDUP + "*", count=1000))
        if dedupe_keys:
            self.redis.delete(*dedupe_keys)
        self.redis.close()

    def _clear_queue_state(self):
        self.redis.delete(
            TaskQueue.STREAM,
            TaskQueue.DLQ,
            TaskQueue.FAIR_WAITING,
            TaskQueue.FAIR_ENTRIES,
            TaskQueue.FAIR_ADMITTED,
            TaskQueue.FAIR_LAST_TENANT,
            TaskQueue.FAIR_STREAK,
        )

    def test_tenant_fair_scheduler_interleaves_backlogged_tenants(self):
        first_started = threading.Event()
        release_first = threading.Event()
        delivered: list[str] = []

        def handler(payload):
            delivered.append(payload["message_id"])
            if len(delivered) == 1:
                first_started.set()
                release_first.wait(5)

        queue = TaskQueue(
            handler,
            workers=1,
            redis_url=REDIS_URL,
            fair_scheduling=True,
        )
        try:
            queue.submit({"message_id": "a1", "tenant_id": "tenant-a"}, "a1")
            self.assertTrue(first_started.wait(5))
            queue.submit({"message_id": "a2", "tenant_id": "tenant-a"}, "a2")
            queue.submit({"message_id": "a3", "tenant_id": "tenant-a"}, "a3")
            queue.submit({"message_id": "b1", "tenant_id": "tenant-b"}, "b1")
            queue.submit({"message_id": "b2", "tenant_id": "tenant-b"}, "b2")
            release_first.set()

            self.assertTrue(_wait(lambda: len(delivered) == 5), delivered)
            self.assertEqual(["a1", "b1", "a2", "b2", "a3"], delivered)
            self.assertEqual(0, queue.fair_waiting_tenants())
            self.assertEqual("uniform-v1", queue.health()["fair_policy_id"])
        finally:
            release_first.set()
            queue.close(3)

    def test_tenant_fair_scheduler_honors_versioned_weights(self):
        first_started = threading.Event()
        release_first = threading.Event()
        delivered: list[str] = []

        def handler(payload):
            delivered.append(payload["message_id"])
            if len(delivered) == 1:
                first_started.set()
                release_first.wait(5)

        with tempfile.NamedTemporaryFile("w", suffix=".toml") as policy:
            policy.write(
                'version = 1\nid = "priority-v1"\ndefault_weight = 1\n[tenants]\n"tenant-a" = 2\n'
            )
            policy.flush()
            queue = TaskQueue(
                handler,
                workers=1,
                redis_url=REDIS_URL,
                fair_scheduling=True,
                tenant_weights_file=policy.name,
            )
            try:
                self.assertEqual(2, queue.tenant_weight("tenant-a"))
                queue.submit({"message_id": "a1", "tenant_id": "tenant-a"}, "a1")
                self.assertTrue(first_started.wait(5))
                queue.submit({"message_id": "a2", "tenant_id": "tenant-a"}, "a2")
                queue.submit({"message_id": "a3", "tenant_id": "tenant-a"}, "a3")
                queue.submit({"message_id": "b1", "tenant_id": "tenant-b"}, "b1")
                release_first.set()

                self.assertTrue(_wait(lambda: len(delivered) == 4), delivered)
                self.assertEqual(["a1", "a2", "b1", "a3"], delivered)
            finally:
                release_first.set()
                queue.close(3)

    def test_tenant_fair_retry_requeues_without_leaking_scheduler_state(self):
        calls = 0
        delivered = threading.Event()

        def handler(_payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient fixture failure")
            delivered.set()

        queue = TaskQueue(
            handler,
            workers=1,
            redis_url=REDIS_URL,
            max_attempts=2,
            fair_scheduling=True,
        )
        try:
            queue.submit({"message_id": "retry", "tenant_id": "tenant-a"}, "retry")
            self.assertTrue(delivered.wait(5))
            self.assertTrue(_wait(lambda: queue.depth() == 0))
            self.assertEqual(2, calls)
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_WAITING))
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_ENTRIES))
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_ADMITTED))
        finally:
            queue.close(3)

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

    def test_fair_entry_reclaim_preserves_single_scheduler_accounting(self):
        self.redis.xgroup_create(TaskQueue.STREAM, TaskQueue.GROUP, id="0", mkstream=True)
        message_id = "fair-crash-" + uuid.uuid4().hex
        tenant_key = hashlib.sha256(b"tenant-a").hexdigest()
        envelope = json.dumps(
            {
                "message_id": message_id,
                "attempt": 0,
                "payload": {"message_id": message_id, "tenant_id": "tenant-a"},
                "submitted_at": time.time(),
                "fair": {
                    "tenant_key": tenant_key,
                    "weight": 1,
                    "policy_sha256": load_tenant_fair_policy().sha256,
                },
            }
        )
        redis_id = self.redis.xadd(TaskQueue.STREAM, {"envelope": envelope})
        self.redis.hincrby(TaskQueue.FAIR_WAITING, tenant_key, 1)
        self.redis.hset(TaskQueue.FAIR_ENTRIES, redis_id, tenant_key)
        script = (
            "import redis,sys; r=redis.Redis.from_url(sys.argv[1],decode_responses=True); "
            "rows=r.xreadgroup(sys.argv[2],'crashed-fair-process',{sys.argv[3]:'>'},count=1); "
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
        time.sleep(1.1)

        delivered = threading.Event()
        queue = TaskQueue(
            lambda payload: delivered.set() if payload["message_id"] == message_id else None,
            workers=1,
            redis_url=REDIS_URL,
            lease_seconds=1,
            fair_scheduling=True,
        )
        try:
            self.assertTrue(delivered.wait(5))
            self.assertTrue(_wait(lambda: queue.depth() == 0))
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_WAITING))
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_ENTRIES))
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_ADMITTED))
        finally:
            queue.close(3)

    def test_fair_marker_without_scheduler_index_fails_closed(self):
        tenant_key = hashlib.sha256(b"tenant-a").hexdigest()
        envelope = json.dumps(
            {
                "message_id": "missing-index",
                "attempt": 0,
                "payload": {"message_id": "missing-index", "tenant_id": "tenant-a"},
                "submitted_at": time.time(),
                "fair": {
                    "tenant_key": tenant_key,
                    "weight": 1,
                    "policy_sha256": load_tenant_fair_policy().sha256,
                },
            }
        )
        self.redis.xadd(TaskQueue.STREAM, {"envelope": envelope})
        delivered = threading.Event()
        queue = TaskQueue(
            lambda _payload: delivered.set(),
            workers=1,
            redis_url=REDIS_URL,
            fair_scheduling=True,
        )
        try:
            self.assertTrue(_wait(lambda: queue.dead_letter_depth() == 1))
            self.assertFalse(delivered.is_set())
            self.assertEqual(0, queue.depth())
        finally:
            queue.close(3)

    def test_reclaimed_fair_marker_must_match_admitted_tenant(self):
        tenant_key = hashlib.sha256(b"tenant-a").hexdigest()
        envelope = json.dumps(
            {
                "message_id": "admitted-mismatch",
                "attempt": 0,
                "payload": {"message_id": "admitted-mismatch", "tenant_id": "tenant-a"},
                "submitted_at": time.time(),
                "fair": {
                    "tenant_key": tenant_key,
                    "weight": 1,
                    "policy_sha256": load_tenant_fair_policy().sha256,
                },
            }
        )
        redis_id = self.redis.xadd(TaskQueue.STREAM, {"envelope": envelope})
        self.redis.hset(TaskQueue.FAIR_ADMITTED, redis_id, hashlib.sha256(b"tenant-b").hexdigest())
        delivered = threading.Event()
        queue = TaskQueue(
            lambda _payload: delivered.set(),
            workers=1,
            redis_url=REDIS_URL,
            fair_scheduling=True,
        )
        try:
            self.assertTrue(_wait(lambda: queue.dead_letter_depth() == 1))
            self.assertFalse(delivered.is_set())
            self.assertEqual(0, queue.depth())
            self.assertEqual(0, self.redis.hlen(TaskQueue.FAIR_ADMITTED))
        finally:
            queue.close(3)

    def test_active_delivery_heartbeat_prevents_false_lease_reclaim(self):
        started = threading.Event()
        release = threading.Event()
        duplicate = threading.Event()
        deliveries = 0
        delivery_lock = threading.Lock()

        def slow_handler(_payload):
            nonlocal deliveries
            with delivery_lock:
                deliveries += 1
                if deliveries > 1:
                    duplicate.set()
            started.set()
            release.wait(5)

        first = TaskQueue(
            slow_handler,
            workers=1,
            redis_url=REDIS_URL,
            lease_seconds=1,
            fair_scheduling=True,
        )
        second = TaskQueue(
            slow_handler,
            workers=1,
            redis_url=REDIS_URL,
            lease_seconds=1,
            fair_scheduling=True,
        )
        try:
            first.submit({"message_id": "slow", "tenant_id": "tenant-a"}, "slow")
            self.assertTrue(started.wait(5))
            self.assertFalse(duplicate.wait(2.2))
            self.assertEqual(1, deliveries)
            self.assertTrue(first.health()["lease_heartbeat_running"])
            release.set()
            self.assertTrue(_wait(lambda: first.depth() == 0))
        finally:
            release.set()
            first.close(3)
            second.close(3)

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


@unittest.skipUnless(
    S3_OBJECT_LOCK_BUCKET and S3_REGION,
    "EVOAGENT_TEST_S3_OBJECT_LOCK_BUCKET/EVOAGENT_TEST_S3_REGION are not configured",
)
class S3ObjectLockRuntimeIntegrationTests(unittest.TestCase):
    def test_bucket_has_object_lock_and_versioning_enabled(self):
        store = S3ObjectLockArtifactStore(
            S3_OBJECT_LOCK_BUCKET,
            region=S3_REGION,
        )
        try:
            self.assertTrue(store.health())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
