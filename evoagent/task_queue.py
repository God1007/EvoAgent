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
        self._memory_dlq: list[dict[str, Any]] = []
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
            self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
            self._redis.ping()
            try:
                self._redis.xgroup_create(self.STREAM, self.GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            for _ in range(workers):
                self._executor.submit(self._redis_worker)

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
            if self._redis:
                self._redis.xadd(
                    self.STREAM, {"envelope": json.dumps(envelope, ensure_ascii=False)}
                )
            else:
                self._schedule_memory(envelope)
        return message_identifier

    def _deliver(self, envelope: dict[str, Any]) -> bool:
        with self._drain_condition:
            self._active_deliveries += 1
        try:
            envelope["attempt"] = int(envelope.get("attempt", 0)) + 1
            try:
                self.handler(envelope["payload"])
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
        while not self._stop.is_set():
            self._reclaim_stale()
            messages = self._redis.xreadgroup(
                self.GROUP, self.consumer, {self.STREAM: ">"}, count=1, block=1000
            )
            for _stream, entries in messages:
                for redis_id, fields in entries:
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
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                        continue
                    try:
                        self._deliver(envelope)
                        # ACK only after work completed or was safely requeued/DLQed.
                        self._redis.xack(self.STREAM, self.GROUP, redis_id)
                    except Exception:
                        # Infrastructure failure: leave pending for lease recovery.
                        continue

    def _reclaim_stale(self) -> None:
        try:
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
                envelope = json.loads(fields["envelope"])
                self._deliver(envelope)
                self._redis.xack(self.STREAM, self.GROUP, redis_id)
        except Exception:
            # Redis versions without XAUTOCLAIM still process new entries.
            return

    def _dead_letter(self, envelope: dict[str, Any], error: str) -> None:
        item = {**envelope, "error": error[:2000], "failed_at": time.time()}
        if self._redis:
            self._redis.xadd(self.DLQ, {"envelope": json.dumps(item, ensure_ascii=False)})
        else:
            with self._lock:
                self._memory_dlq.append(item)
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
                self.submit(payload, message_id=message_id)
                return True
        return False

    def depth(self) -> int:
        """Best-effort backlog size for observability. For Redis this is the
        stream length (approximate: includes un-trimmed acked entries); for the
        in-memory backend it is the executor queue size. Returns -1 if unknown."""
        try:
            if self._redis:
                return int(self._redis.xlen(self.STREAM))
            with self._drain_condition:
                return self._scheduled_memory
        except Exception:  # pragma: no cover - defensive probe isolation
            return -1

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
        return drained
