"""Task delivery with an optional durable Redis Streams backend."""

from __future__ import annotations

import json
import math
import socket
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .errors import coerce_safe_summary, safe_exception_summary
from .json_boundary import strict_json_loads
from .metrics import metrics


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class TaskQueue:
    """Deliver tasks in-process or through one Redis Streams consumer group."""

    STREAM = "evoagent:review:stream"
    DLQ = "evoagent:review:dlq"
    DEDUP = "evoagent:review:dedup:"
    # ponytail: fixed one-hour window; add a bounded index for longer recovery.
    DEDUP_TTL_SECONDS = 60 * 60
    # ponytail: fixed incident buffer; move the DLQ to durable tenant storage if 10k is too small.
    MAX_DLQ_MESSAGES = 10_000
    # ponytail: queue messages are metadata; raise only if valid task context outgrows 256 KiB.
    MAX_ENVELOPE_BYTES = 256 * 1024
    MAX_DLQ_ENTRY_BYTES = MAX_ENVELOPE_BYTES + 4096
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
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 256:
            raise ValueError("task queue workers must be an integer between 1 and 256")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (max_attempts, lease_seconds)
        ):
            raise ValueError("task queue attempts and lease must be positive integers")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in (backoff_base, backoff_cap)
        ):
            raise ValueError("task queue backoff limits must be positive and finite")
        self.handler = handler
        self.redis_url = redis_url
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.on_dead_letter = on_dead_letter
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self._backoff_cap_exponent = max(
            0, math.ceil(math.log2(backoff_cap) - math.log2(backoff_base))
        )
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="evoagent-worker"
        )
        self._redis: Any = None
        self._redis_workers: list[Future[Any]] = []
        self._redis_heartbeat: threading.Thread | None = None
        self._redis_active_ids: set[str] = set()
        self._last_worker_error = ""
        self._last_heartbeat_error = ""
        self._memory_dlq: deque[dict[str, Any]] = deque(maxlen=self.MAX_DLQ_MESSAGES)
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
            if not isinstance(payload, dict):
                raise ValueError("task queue payload must be an object")
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

    def _validate_envelope(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("task queue envelope must be an object")
        message_id = value.get("message_id")
        attempt = value.get("attempt")
        submitted_at = value.get("submitted_at")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("task queue message_id must be a non-empty string")
        if (
            isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or not 0 <= attempt < self.max_attempts
        ):
            raise ValueError("task queue attempt is outside the retry budget")
        if not isinstance(value.get("payload"), dict):
            raise ValueError("task queue payload must be an object")
        if (
            isinstance(submitted_at, bool)
            or not isinstance(submitted_at, (int, float))
            or not math.isfinite(submitted_at)
            or submitted_at < 0
        ):
            raise ValueError("task queue submitted_at must be finite and non-negative")
        return value

    @staticmethod
    def _bounded_json(value: Any, limit: int) -> Any:
        if not isinstance(value, (str, bytes)):
            raise ValueError("task queue JSON must be text")
        encoded = value.encode("utf-8") if isinstance(value, str) else value
        if len(encoded) > limit:
            raise ValueError("task queue JSON exceeds the byte limit")
        return strict_json_loads(value)

    def _publish_envelope(self, envelope: dict[str, Any], deduplicate: bool) -> None:
        envelope = self._validate_envelope(envelope)
        serialized = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        if self._bounded_json(serialized, self.MAX_ENVELOPE_BYTES) != envelope:
            raise ValueError("task queue values must use JSON-native types")
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
                else:
                    exponent = envelope["attempt"] - 1
                    delay = (
                        self.backoff_cap
                        if exponent >= self._backoff_cap_exponent
                        else min(math.ldexp(self.backoff_base, exponent), self.backoff_cap)
                    )
                    if self._redis:
                        # ponytail: one bounded worker sleeps; add delayed delivery only if
                        # retries measurably starve fresh queue traffic.
                        self._stop.wait(delay)
                        self._publish_envelope(envelope, deduplicate=False)
                    else:
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

    def _memory_done(self, future: Future[Any]) -> None:
        if not future.cancelled() and (error := future.exception()) is not None:
            self._last_worker_error = safe_exception_summary(error, "queue dependency failed")
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
                self._last_worker_error = ""
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
        with self._lifecycle_lock:
            if self._stop.is_set():
                return
            with self._drain_condition:
                if redis_id in self._redis_active_ids:
                    return
                self._redis_active_ids.add(redis_id)
        try:
            try:
                envelope = self._validate_envelope(
                    self._bounded_json(fields["envelope"], self.MAX_ENVELOPE_BYTES)
                )
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
            serialized = json.dumps(item, ensure_ascii=False, allow_nan=False)
            if self._bounded_json(serialized, self.MAX_DLQ_ENTRY_BYTES) != item:
                raise ValueError("task queue values must use JSON-native types")
            self._redis.xadd(
                self.DLQ,
                {"envelope": serialized},
                maxlen=self.MAX_DLQ_MESSAGES,
                approximate=True,
            )
        else:
            with self._lock:
                self._memory_dlq.append(item)
                self._memory_submission_times.pop(str(envelope.get("message_id", "")), None)
        if self.on_dead_letter:
            raw_payload = envelope.get("payload")
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
            payload["_queue_message_id"] = str(envelope.get("message_id") or "")
            self.on_dead_letter(payload, item["error"])

    def dead_letters(self, limit: int = 100) -> list:
        fetch_limit = max(1, min(limit, 500))
        if self._redis:
            rows = self._redis.xrevrange(self.DLQ, count=fetch_limit)
            values = []
            for _redis_id, fields in rows:
                try:
                    item = self._bounded_json(fields["envelope"], self.MAX_DLQ_ENTRY_BYTES)
                    if not isinstance(item, dict):
                        raise ValueError("dead letter must be an object")
                except Exception:
                    metrics.inc("queue_dead_letter_decode_failures_total")
                    continue
                values.append(item)
        else:
            with self._lock:
                values = list(reversed(self._memory_dlq))[:fetch_limit]
        return [
            {**item, "error": coerce_safe_summary(item.get("error"), "task delivery failed")}
            for item in values
        ]

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
                    submitted_at = float(
                        self._bounded_json(fields["envelope"], self.MAX_ENVELOPE_BYTES)[
                            "submitted_at"
                        ]
                    )
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
            error = self._last_worker_error
            return {
                "healthy": not self._stop.is_set() and not error,
                "backend": self.backend,
                "workers_running": 0,
                "workers_expected": 0,
                "last_error": error,
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
