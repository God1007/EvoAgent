"""Task delivery with retry backoff and a dead-letter queue.

Two backends with **different durability guarantees**:

* ``redis-streams`` (production): durable. Messages survive process restarts;
  delivery uses consumer-group ACK and lease-based reclaim of stale (crashed
  worker) messages via ``XAUTOCLAIM``, so in-flight work is not lost.
* ``memory-ephemeral`` (single process / development): **not durable**. Work is
  held only in an in-process executor queue and the dead-letter list, so any
  pending, in-flight, retry-scheduled, or dead-lettered task is lost if the
  process exits. There is no ACK or lease recovery in this backend.

Both backends share the same retry-with-backoff and dead-letter API surface;
only the Redis backend provides at-least-once durable delivery.

Shutdown first rejects new submissions, then performs a bounded drain of active
deliveries. Already-scheduled memory work is included in the drain count; Redis
messages that are not completed remain durable for lease-based recovery.
"""

import json
import socket
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class TaskQueue:
    STREAM = "evoagent:review:stream"
    DLQ = "evoagent:review:dlq"
    DEDUP = "evoagent:review:dedup:"
    DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60
    GROUP = "evoagent-workers"

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], None],
        workers: int = 2,
        redis_url: str = "",
        max_attempts: int = 3,
        lease_seconds: int = 60,
        on_dead_letter: Callable[[dict[str, Any], str], None] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 10.0,
    ):
        self.handler = handler
        self.redis_url = redis_url
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.on_dead_letter = on_dead_letter
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="evoagent-worker"
        )
        self._redis: Any = None
        self._redis_workers: list[Future[Any]] = []
        self._last_worker_error = ""
        self._memory_dlq: list[dict[str, Any]] = []
        self._memory_published_ids: dict[str, float] = {}
        self._memory_submission_times: dict[str, float] = {}
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._drain_condition = threading.Condition()
        self._active_deliveries = 0
        self._scheduled_memory = 0
        self._stop = threading.Event()
        self.consumer = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
        if redis_url:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError("Redis mode requires: pip install redis") from exc
            self._redis = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=max(2, min(lease_seconds, 5)),
                health_check_interval=30,
            )
            self._redis.ping()
            try:
                self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            for _ in range(workers):
                self._redis_workers.append(self._executor.submit(self._redis_worker))

    @property
    def backend(self) -> str:
        return "redis-streams" if self._redis else "memory-ephemeral"

    @property
    def durable(self) -> bool:
        """Whether the active backend survives a process restart."""
        return self._redis is not None

    def submit(self, payload: dict[str, Any], message_id: str = "") -> str:
        with self._lifecycle_lock:
            if self._stop.is_set():
                raise RuntimeError("task queue is closed")
            message_identifier = message_id or str(payload.get("task_id") or uuid.uuid4())
            envelope: dict[str, Any] = {
                "message_id": message_identifier,
                "attempt": 0,
                "payload": payload,
                "submitted_at": time.time(),
            }
            self._publish_envelope(envelope, deduplicate=True)
        return message_identifier

    def _publish_envelope(self, envelope: dict[str, Any], deduplicate: bool) -> None:
        serialized = json.dumps(envelope, ensure_ascii=False)
        message_id = str(envelope["message_id"])
        if self._redis:
            if deduplicate:
                # HGET + XADD + HSET must be one Redis operation. A process
                # crash cannot leave a dedupe marker without the stream entry,
                # and an outbox retry after publish returns without duplicating.
                self._redis.eval(
                    "local key=KEYS[1]..ARGV[1]; "
                    "local existing=redis.call('GET',key); "
                    "if existing then return existing end; "
                    "local id=redis.call('XADD',KEYS[2],'*','envelope',ARGV[2]); "
                    "redis.call('SET',key,id,'EX',ARGV[3]); return id",
                    2,
                    self.DEDUP,
                    self.STREAM,
                    message_id,
                    serialized,
                    self.DEDUP_TTL_SECONDS,
                )
            else:
                self._redis.xadd(self.STREAM, {"envelope": serialized})
            return
        if deduplicate:
            now = time.monotonic()
            published_at = self._memory_published_ids.get(message_id)
            if published_at is not None and now - published_at < self.DEDUP_TTL_SECONDS:
                return
            if len(self._memory_published_ids) >= 100_000:
                cutoff = now - self.DEDUP_TTL_SECONDS
                self._memory_published_ids = {
                    key: timestamp
                    for key, timestamp in self._memory_published_ids.items()
                    if timestamp >= cutoff
                }
                while len(self._memory_published_ids) >= 100_000:
                    self._memory_published_ids.pop(next(iter(self._memory_published_ids)))
        if deduplicate:
            self._memory_published_ids[message_id] = time.monotonic()
        with self._lock:
            self._memory_submission_times.setdefault(
                message_id, float(envelope.get("submitted_at") or time.time())
            )
        try:
            self._schedule_memory(envelope)
        except Exception:
            with self._lock:
                self._memory_submission_times.pop(message_id, None)
            if deduplicate:
                self._memory_published_ids.pop(message_id, None)
            raise

    def _deliver(self, envelope: dict[str, Any]) -> bool:
        with self._drain_condition:
            self._active_deliveries += 1
        try:
            envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
            try:
                self.handler(envelope["payload"])
                self._forget_memory_envelope(envelope)
                return True
            except PermanentTaskError as exc:
                self._dead_letter(envelope, str(exc))
                return False
            except Exception as exc:
                if envelope["attempt"] >= self.max_attempts:
                    self._dead_letter(envelope, str(exc))
                elif self._redis:
                    self._redis.xadd(
                        self.STREAM,
                        {"envelope": json.dumps(envelope, ensure_ascii=False)},
                    )
                else:
                    delay = min(
                        self.backoff_base * 2 ** (envelope["attempt"] - 1),
                        self.backoff_cap,
                    )
                    timer = threading.Timer(delay, self._schedule_memory, args=(envelope,))
                    timer.daemon = True
                    timer.start()
                return False
        finally:
            with self._drain_condition:
                self._active_deliveries -= 1
                self._drain_condition.notify_all()

    def _schedule_memory(self, envelope: dict[str, Any]) -> None:
        with self._lifecycle_lock:
            if self._stop.is_set():
                return
            with self._drain_condition:
                self._scheduled_memory += 1
            try:
                future = self._executor.submit(self._deliver_memory, envelope)
                future.add_done_callback(self._memory_done)
            except RuntimeError:
                with self._drain_condition:
                    self._scheduled_memory -= 1
                    self._drain_condition.notify_all()
                if not self._stop.is_set():
                    raise

    def _deliver_memory(self, envelope: dict[str, Any]) -> None:
        self._deliver(envelope)

    def _memory_done(self, _future: Future[Any]) -> None:
        with self._drain_condition:
            self._scheduled_memory -= 1
            self._drain_condition.notify_all()

    def _redis_worker(self) -> None:
        retry_delay = 0.1
        while not self._stop.is_set():
            try:
                self._reclaim_stale()
                messages = self._redis.xreadgroup(
                    self.GROUP, self.consumer, {self.STREAM: ">"}, count=1, block=1000
                )
                for _stream, entries in messages:
                    for redis_id, fields in entries:
                        self._consume_redis_entry(redis_id, fields)
                self._last_worker_error = ""
                retry_delay = 0.1
            except Exception as exc:
                # A Redis restart or transient network failure must not silently
                # retire the worker Future forever. Leave unacknowledged entries
                # pending and reconnect with bounded exponential backoff.
                self._last_worker_error = str(exc)[:500]
                self._stop.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)

    def _consume_redis_entry(self, redis_id: str, fields: dict[str, Any]) -> None:
        try:
            envelope = json.loads(fields["envelope"])
        except Exception as exc:
            envelope = {
                "message_id": redis_id,
                "attempt": self.max_attempts,
                "payload": {},
                "submitted_at": time.time(),
            }
            self._dead_letter(envelope, "invalid queue envelope: %s" % exc)
            self._ack_redis_entry(redis_id)
            return
        self._deliver(envelope)
        # ACK only after work completed or was safely requeued/DLQed. XDEL is
        # safe because EvoAgent owns the only consumer group for this stream;
        # retaining acknowledged rows forever would make queue depth and Redis
        # memory usage grow without bound.
        self._ack_redis_entry(redis_id)

    def _ack_redis_entry(self, redis_id: str) -> None:
        with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(self.STREAM, self.GROUP, redis_id)
            pipeline.xdel(self.STREAM, redis_id)
            pipeline.execute()

    def _reclaim_stale(self) -> None:
        result = self._redis.xautoclaim(
            self.STREAM,
            self.GROUP,
            self.consumer,
            min_idle_time=self.lease_seconds * 1000,
            start_id="0-0",
            count=10,
        )
        entries = result[1] if len(result) > 1 else []
        for redis_id, fields in entries:
            self._consume_redis_entry(redis_id, fields)

    def _dead_letter(self, envelope: dict[str, Any], error: str) -> None:
        item = {**envelope, "error": error[:2000], "failed_at": time.time()}
        if self._redis:
            self._redis.xadd(self.DLQ, {"envelope": json.dumps(item, ensure_ascii=False)})
        else:
            with self._lock:
                self._memory_dlq.append(item)
                self._memory_submission_times.pop(str(envelope.get("message_id", "")), None)
        if self.on_dead_letter:
            self.on_dead_letter(envelope.get("payload") or {}, item["error"])

    def dead_letters(self, limit: int = 100) -> list:
        if self._redis:
            rows = self._redis.xrevrange(self.DLQ, count=max(1, min(limit, 500)))
            return [json.loads(fields["envelope"]) for _id, fields in rows]
        with self._lock:
            return list(reversed(self._memory_dlq[-limit:]))

    def replay_dead_letter(self, message_id: str) -> bool:
        for item in self.dead_letters(500):
            if item.get("message_id") == message_id:
                payload = item.get("payload") or {}
                with self._lifecycle_lock:
                    if self._stop.is_set():
                        raise RuntimeError("task queue is closed")
                    self._publish_envelope(
                        {
                            "message_id": message_id,
                            "attempt": 0,
                            "payload": payload,
                            "submitted_at": time.time(),
                        },
                        deduplicate=False,
                    )
                return True
        return False

    def depth(self) -> int:
        """Best-effort backlog size for observability. For Redis this is the
        unacknowledged stream length (acknowledged rows are deleted); for the
        in-memory backend it is scheduled executor work. Returns -1 if unknown."""
        try:
            if self._redis:
                return int(self._redis.xlen(self.STREAM))
            with self._drain_condition:
                return self._scheduled_memory
        except Exception:  # pragma: no cover - defensive probe isolation
            return -1

    def oldest_age_seconds(self) -> float:
        """Age of the oldest unacknowledged message, or -1 when unknown."""
        try:
            if self._redis:
                rows = self._redis.xrange(self.STREAM, min="-", max="+", count=1)
                if not rows:
                    return 0.0
                redis_id, fields = rows[0]
                try:
                    submitted_at = float(json.loads(fields["envelope"])["submitted_at"])
                except Exception:
                    submitted_at = int(str(redis_id).split("-", 1)[0]) / 1000.0
                return max(0.0, time.time() - submitted_at)
            with self._lock:
                if not self._memory_submission_times:
                    return 0.0
                return max(0.0, time.time() - min(self._memory_submission_times.values()))
        except Exception:  # pragma: no cover - defensive probe isolation
            return -1.0

    def dead_letter_depth(self) -> int:
        try:
            if self._redis:
                return int(self._redis.xlen(self.DLQ))
            with self._lock:
                return len(self._memory_dlq)
        except Exception:  # pragma: no cover - defensive probe isolation
            return -1

    def _forget_memory_envelope(self, envelope: dict[str, Any]) -> None:
        if self._redis:
            return
        with self._lock:
            self._memory_submission_times.pop(str(envelope.get("message_id", "")), None)

    def health(self) -> dict[str, Any]:
        """Return a dependency and worker-liveness snapshot for readiness."""
        if not self._redis:
            return {
                "healthy": not self._stop.is_set(),
                "backend": self.backend,
                "workers_running": 0,
                "workers_expected": 0,
                "last_error": "",
            }
        running = sum(not worker.done() for worker in self._redis_workers)
        dependency_ok = False
        error = self._last_worker_error
        try:
            dependency_ok = bool(self._redis.ping())
        except Exception as exc:
            error = str(exc)[:500]
        expected = len(self._redis_workers)
        return {
            "healthy": dependency_ok and running == expected and not self._stop.is_set(),
            "backend": self.backend,
            "workers_running": running,
            "workers_expected": expected,
            "last_error": error,
        }

    def drain(self, timeout_seconds: float = 0.0) -> bool:
        """Wait for active and already-scheduled in-memory deliveries.

        Retry timers are not kept alive during shutdown. Redis messages remain
        durable and can be reclaimed by another consumer after their lease.
        """
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._drain_condition:
            while self._active_deliveries or self._scheduled_memory:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drain_condition.wait(remaining)
            return True

    def close(self, drain_timeout_seconds: float = 0.0) -> bool:
        """Stop intake, drain bounded in-flight work, and release workers."""
        with self._lifecycle_lock:
            self._stop.set()
        drained = self.drain(drain_timeout_seconds)
        self._executor.shutdown(wait=False, cancel_futures=not drained)
        if self._redis is not None and drained:
            # Closing the pool interrupts a worker blocked in XREADGROUP; its
            # reconnect loop observes `_stop` and exits without a leaked socket.
            self._redis.close()
        return drained
