"""Task delivery with an optional durable Redis Streams backend."""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .errors import coerce_safe_summary, safe_exception_summary
from .metrics import metrics


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class TaskQueue:
    """Deliver tasks in-process or through one Redis Streams consumer group."""

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
        self._redis_heartbeat: threading.Thread | None = None
        self._redis_active_ids: set[str] = set()
        self._last_worker_error = ""
        self._last_heartbeat_error = ""
        self._memory_dlq: list[dict[str, Any]] = []
        self._memory_published_ids: dict[str, float] = {}
        self._memory_submission_times: dict[str, float] = {}
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._drain_condition = threading.Condition()
        self._active_deliveries = 0
        self._scheduled_memory = 0
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self.consumer = "%s-%s" % (socket.gethostname(), uuid.uuid4().hex[:8])
        if redis_url:
            self._connect_redis(workers)

    def _connect_redis(self, workers: int) -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Redis mode requires: pip install redis") from exc
        try:
            self._redis = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=max(2, min(self.lease_seconds, 5)),
                health_check_interval=30,
            )
            self._redis.ping()
            try:
                self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        except Exception:
            if self._redis is not None:
                self._redis.close()
            self._redis = None
            self._executor.shutdown(wait=False, cancel_futures=True)
            raise
        self._redis_workers = [self._executor.submit(self._redis_worker) for _ in range(workers)]
        self._redis_heartbeat = threading.Thread(
            target=self._redis_lease_heartbeat,
            name="evoagent-queue-lease-heartbeat",
            daemon=True,
        )
        self._redis_heartbeat.start()

    @property
    def backend(self) -> str:
        return "redis-streams" if self._redis else "memory-ephemeral"

    @property
    def durable(self) -> bool:
        return self._redis is not None

    def submit(self, payload: dict[str, Any], message_id: str = "") -> str:
        with self._lifecycle_lock:
            if self._stop.is_set():
                raise RuntimeError("task queue is closed")
            identifier = message_id or str(payload.get("task_id") or uuid.uuid4())
            self._publish_envelope(
                {
                    "message_id": identifier,
                    "attempt": 0,
                    "payload": payload,
                    "submitted_at": time.time(),
                },
                deduplicate=True,
            )
        return identifier

    def _publish_envelope(self, envelope: dict[str, Any], deduplicate: bool) -> None:
        serialized = json.dumps(envelope, ensure_ascii=False)
        message_id = str(envelope["message_id"])
        if self._redis:
            if deduplicate:
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
            self._memory_published_ids[message_id] = now
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

    def _deliver(self, envelope: dict[str, Any]) -> None:
        with self._drain_condition:
            self._active_deliveries += 1
        try:
            envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
            try:
                self.handler(envelope["payload"])
                self._forget_memory_envelope(envelope)
            except PermanentTaskError as exc:
                self._dead_letter(envelope, safe_exception_summary(exc, "task delivery failed"))
            except Exception as exc:
                if envelope["attempt"] >= self.max_attempts:
                    self._dead_letter(envelope, safe_exception_summary(exc, "task delivery failed"))
                elif self._redis:
                    self._publish_envelope(envelope, deduplicate=False)
                else:
                    delay = min(
                        self.backoff_base * 2 ** (envelope["attempt"] - 1),
                        self.backoff_cap,
                    )
                    timer = threading.Timer(delay, self._schedule_memory, args=(envelope,))
                    timer.daemon = True
                    timer.start()
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
                future = self._executor.submit(self._deliver, envelope)
                future.add_done_callback(self._memory_done)
            except RuntimeError:
                with self._drain_condition:
                    self._scheduled_memory -= 1
                    self._drain_condition.notify_all()
                if not self._stop.is_set():
                    raise

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
                        if self._stop.is_set():
                            return
                        self._consume_redis_entry(redis_id, fields)
                self._last_worker_error = ""
                retry_delay = 0.1
            except Exception as exc:
                self._last_worker_error = safe_exception_summary(exc, "queue dependency failed")
                self._stop.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)

    def _consume_redis_entry(self, redis_id: str, fields: dict[str, Any]) -> None:
        try:
            envelope = json.loads(fields["envelope"])
            if not isinstance(envelope, dict):
                raise ValueError("task queue envelope must be an object")
        except Exception as exc:
            envelope = {
                "message_id": redis_id,
                "attempt": self.max_attempts,
                "payload": {},
                "submitted_at": time.time(),
            }
            self._dead_letter(envelope, safe_exception_summary(exc, "task delivery failed"))
            self._ack_redis_entry(redis_id)
            return
        with self._drain_condition:
            self._redis_active_ids.add(redis_id)
        try:
            self._deliver(envelope)
            self._ack_redis_entry(redis_id)
        finally:
            with self._drain_condition:
                self._redis_active_ids.discard(redis_id)
                self._drain_condition.notify_all()

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

    def _redis_lease_heartbeat(self) -> None:
        interval = max(0.1, min(5.0, self.lease_seconds / 3.0))
        while not self._heartbeat_stop.wait(interval):
            with self._drain_condition:
                active_ids = tuple(self._redis_active_ids)
            if not active_ids:
                self._last_heartbeat_error = ""
                if self._stop.is_set():
                    return
                continue
            try:
                self._redis.xclaim(
                    self.STREAM,
                    self.GROUP,
                    self.consumer,
                    min_idle_time=0,
                    message_ids=active_ids,
                    idle=0,
                    justid=True,
                )
                self._last_heartbeat_error = ""
            except Exception as exc:
                self._last_heartbeat_error = safe_exception_summary(exc, "queue dependency failed")
                metrics.inc("queue_lease_heartbeat_failures_total")

    def _dead_letter(self, envelope: dict[str, Any], error: str) -> None:
        error = coerce_safe_summary(error, "task delivery failed")
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
            values = [json.loads(fields["envelope"]) for _id, fields in rows]
        else:
            with self._lock:
                values = list(reversed(self._memory_dlq[-limit:]))
        return [
            {**item, "error": coerce_safe_summary(item.get("error"), "task delivery failed")}
            for item in values
        ]

    def replay_dead_letter(self, message_id: str) -> bool:
        for item in self.dead_letters(500):
            if item.get("message_id") == message_id:
                with self._lifecycle_lock:
                    if self._stop.is_set():
                        raise RuntimeError("task queue is closed")
                    self._publish_envelope(
                        {
                            "message_id": message_id,
                            "attempt": 0,
                            "payload": item.get("payload") or {},
                            "submitted_at": time.time(),
                        },
                        deduplicate=False,
                    )
                return True
        return False

    def depth(self) -> int:
        try:
            if self._redis:
                return int(self._redis.xlen(self.STREAM))
            with self._drain_condition:
                return self._scheduled_memory
        except Exception:
            return -1

    def oldest_age_seconds(self) -> float:
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
        except Exception:
            return -1.0

    def dead_letter_depth(self) -> int:
        try:
            if self._redis:
                return int(self._redis.xlen(self.DLQ))
            with self._lock:
                return len(self._memory_dlq)
        except Exception:
            return -1

    def _forget_memory_envelope(self, envelope: dict[str, Any]) -> None:
        if not self._redis:
            with self._lock:
                self._memory_submission_times.pop(str(envelope.get("message_id", "")), None)

    def health(self) -> dict[str, Any]:
        if not self._redis:
            return {
                "healthy": not self._stop.is_set(),
                "backend": self.backend,
                "workers_running": 0,
                "workers_expected": 0,
                "last_error": "",
                "lease_heartbeat_running": False,
            }
        running = sum(not worker.done() for worker in self._redis_workers)
        error = self._last_worker_error or self._last_heartbeat_error
        try:
            dependency_ok = bool(self._redis.ping())
        except Exception as exc:
            dependency_ok = False
            error = safe_exception_summary(exc, "queue dependency failed")
        if error:
            error = coerce_safe_summary(error, "queue dependency failed")
        expected = len(self._redis_workers)
        heartbeat_running = bool(
            self._redis_heartbeat is not None and self._redis_heartbeat.is_alive()
        )
        return {
            "healthy": (
                dependency_ok
                and running == expected
                and heartbeat_running
                and not error
                and not self._stop.is_set()
            ),
            "backend": self.backend,
            "workers_running": running,
            "workers_expected": expected,
            "last_error": error,
            "lease_heartbeat_running": heartbeat_running,
        }

    def drain(self, timeout_seconds: float = 0.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._drain_condition:
            while self._active_deliveries or self._scheduled_memory or self._redis_active_ids:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drain_condition.wait(remaining)
            return True

    def close(self, drain_timeout_seconds: float = 0.0) -> bool:
        with self._lifecycle_lock:
            self._stop.set()
        drained = self.drain(drain_timeout_seconds)
        if drained:
            self._heartbeat_stop.set()
        if self._redis_heartbeat is not None and drained:
            self._redis_heartbeat.join(timeout=max(0.1, min(1.0, drain_timeout_seconds or 1.0)))
        self._executor.shutdown(wait=False, cancel_futures=not drained)
        if self._redis is not None and drained:
            self._redis.close()
        return drained
