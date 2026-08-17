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

Redis can additionally coordinate content-addressed weighted tenant turns. The
stream remains the source of truth: Lua only grants the next handler start or
atomically moves an over-quota tenant entry to the tail. Waiting/admitted indexes
make retries and lease reclaim single-accounted across replicas. Legacy
unmarked envelopes remain consumable during a two-stage rollout.

Shutdown first rejects new submissions, then performs a bounded drain of active
deliveries. Already-scheduled memory work is included in the drain count; Redis
messages that are not completed remain durable for lease-based recovery.
"""

import hashlib
import json
import re
import socket
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .errors import coerce_safe_summary, safe_exception_summary
from .metrics import metrics

_TENANT_KEY = re.compile(r"^[0-9a-f]{64}$")
_POLICY_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POLICY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class TenantFairPolicy:
    policy_id: str
    default_weight: int
    tenant_weights: MappingProxyType
    sha256: str

    def weight(self, tenant_id: str) -> int:
        return int(self.tenant_weights.get(tenant_id, self.default_weight))


def load_tenant_fair_policy(path: str = "") -> TenantFairPolicy:
    """Load a bounded, content-addressed v1 tenant scheduling policy."""
    document: dict[str, Any] = {
        "version": 1,
        "id": "uniform-v1",
        "default_weight": 1,
        "tenants": {},
    }
    if path:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
        except OSError as exc:
            raise ValueError("cannot read EVOAGENT_QUEUE_TENANT_WEIGHTS_FILE: %s" % exc) from exc
        if len(raw) > 1024 * 1024:
            raise ValueError("tenant queue weights file exceeds the 1 MiB limit")
        try:
            parsed = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("tenant queue weights file is not valid TOML") from exc
        if not isinstance(parsed, dict):  # pragma: no cover - tomllib returns a mapping
            raise ValueError("tenant queue weights file must be a TOML document")
        document = parsed
    unknown = set(document).difference({"version", "id", "default_weight", "tenants"})
    if unknown:
        raise ValueError("tenant queue weights file contains unsupported top-level fields")
    if document.get("version") != 1:
        raise ValueError("tenant queue weights file version must be 1")
    policy_id = document.get("id")
    if not isinstance(policy_id, str) or not _POLICY_ID.fullmatch(policy_id):
        raise ValueError("tenant queue weights file id must be a stable identifier")
    default_weight = _queue_weight(document.get("default_weight", 1), "default_weight")
    raw_tenants = document.get("tenants", {})
    if not isinstance(raw_tenants, dict):
        raise ValueError("tenant queue weights tenants must be a TOML table")
    if len(raw_tenants) > 1000:
        raise ValueError("tenant queue weights file accepts at most 1000 tenants")
    tenant_weights: dict[str, int] = {}
    for tenant_id, value in raw_tenants.items():
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
            or len(tenant_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in tenant_id)
        ):
            raise ValueError("tenant queue weight keys must be bounded tenant identifiers")
        tenant_weights[tenant_id] = _queue_weight(value, "tenant %s" % tenant_id)
    canonical = json.dumps(
        {
            "version": 1,
            "id": policy_id,
            "default_weight": default_weight,
            "tenants": tenant_weights,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return TenantFairPolicy(
        policy_id,
        default_weight,
        MappingProxyType(tenant_weights),
        hashlib.sha256(canonical).hexdigest(),
    )


def _queue_weight(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("tenant queue %s weight must be an integer between 1 and 100" % field)
    return value


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class TaskQueue:
    STREAM = "evoagent:review:stream"
    DLQ = "evoagent:review:dlq"
    DEDUP = "evoagent:review:dedup:"
    DEDUP_TTL_SECONDS = 7 * 24 * 60 * 60
    GROUP = "evoagent-workers"
    FAIR_WAITING = "evoagent:review:fair:waiting"
    FAIR_ENTRIES = "evoagent:review:fair:entries"
    FAIR_ADMITTED = "evoagent:review:fair:admitted"
    FAIR_LAST_TENANT = "evoagent:review:fair:last-tenant"
    FAIR_STREAK = "evoagent:review:fair:streak"

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
        fair_scheduling: bool = False,
        tenant_weights_file: str = "",
    ):
        if fair_scheduling and not redis_url:
            raise ValueError("tenant-fair scheduling requires EVOAGENT_REDIS_URL")
        self._fair_policy = load_tenant_fair_policy(tenant_weights_file)
        self._fair_scheduling = bool(fair_scheduling)
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
        """Whether the active backend survives a process restart."""
        return self._redis is not None

    @property
    def fair_scheduling(self) -> bool:
        return self._fair_scheduling

    @property
    def fair_policy_id(self) -> str:
        return self._fair_policy.policy_id

    def tenant_weight(self, tenant_id: str) -> int:
        return self._fair_policy.weight(tenant_id)

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
            self._decorate_fair_envelope(envelope)
            self._publish_envelope(envelope, deduplicate=True)
        return message_identifier

    def _publish_envelope(self, envelope: dict[str, Any], deduplicate: bool) -> None:
        self._decorate_fair_envelope(envelope)
        serialized = json.dumps(envelope, ensure_ascii=False)
        message_id = str(envelope["message_id"])
        if self._redis:
            fair = self._fair_marker(envelope)
            if deduplicate:
                # HGET + XADD + HSET must be one Redis operation. A process
                # crash cannot leave a dedupe marker without the stream entry,
                # and an outbox retry after publish returns without duplicating.
                if fair:
                    self._redis.eval(
                        "local key=KEYS[1]..ARGV[1]; "
                        "local existing=redis.call('GET',key); "
                        "if existing then return existing end; "
                        "local id=redis.call('XADD',KEYS[2],'*','envelope',ARGV[2]); "
                        "redis.call('SET',key,id,'EX',ARGV[3]); "
                        "redis.call('HINCRBY',KEYS[3],ARGV[4],1); "
                        "redis.call('HSET',KEYS[4],id,ARGV[4]); return id",
                        4,
                        self.DEDUP,
                        self.STREAM,
                        self.FAIR_WAITING,
                        self.FAIR_ENTRIES,
                        message_id,
                        serialized,
                        self.DEDUP_TTL_SECONDS,
                        fair["tenant_key"],
                    )
                else:
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
            elif fair:
                self._redis.eval(
                    "local id=redis.call('XADD',KEYS[1],'*','envelope',ARGV[1]); "
                    "redis.call('HINCRBY',KEYS[2],ARGV[2],1); "
                    "redis.call('HSET',KEYS[3],id,ARGV[2]); return id",
                    3,
                    self.STREAM,
                    self.FAIR_WAITING,
                    self.FAIR_ENTRIES,
                    serialized,
                    fair["tenant_key"],
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

    def _decorate_fair_envelope(self, envelope: dict[str, Any]) -> None:
        if not self._fair_scheduling or "fair" in envelope:
            return
        payload = envelope.get("payload")
        tenant_id = (
            str(payload.get("tenant_id") or "default") if isinstance(payload, dict) else "default"
        )
        envelope["fair"] = {
            "tenant_key": hashlib.sha256(tenant_id.encode()).hexdigest(),
            "weight": self._fair_policy.weight(tenant_id),
            "policy_sha256": self._fair_policy.sha256,
        }

    @staticmethod
    def _fair_marker(envelope: dict[str, Any]) -> dict[str, Any] | None:
        fair = envelope.get("fair")
        if fair is None:
            return None
        if not isinstance(fair, dict):
            raise ValueError("task queue fair marker must be an object")
        tenant_key = fair.get("tenant_key")
        weight = fair.get("weight")
        policy_sha256 = fair.get("policy_sha256")
        if not isinstance(tenant_key, str) or not _TENANT_KEY.fullmatch(tenant_key):
            raise ValueError("task queue fair marker has an invalid tenant key")
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 100:
            raise ValueError("task queue fair marker has an invalid weight")
        if not isinstance(policy_sha256, str) or not _POLICY_SHA256.fullmatch(policy_sha256):
            raise ValueError("task queue fair marker has an invalid policy digest")
        return fair

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
                self._dead_letter(
                    envelope,
                    safe_exception_summary(exc, "task delivery failed"),
                )
                return False
            except Exception as exc:
                if envelope["attempt"] >= self.max_attempts:
                    self._dead_letter(
                        envelope,
                        safe_exception_summary(exc, "task delivery failed"),
                    )
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
                        if self._stop.is_set():
                            return
                        self._consume_redis_entry(redis_id, fields)
                self._last_worker_error = ""
                retry_delay = 0.1
            except Exception as exc:
                # A Redis restart or transient network failure must not silently
                # retire the worker Future forever. Leave unacknowledged entries
                # pending and reconnect with bounded exponential backoff.
                self._last_worker_error = safe_exception_summary(exc, "queue dependency failed")
                self._stop.wait(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)

    def _consume_redis_entry(self, redis_id: str, fields: dict[str, Any]) -> None:
        try:
            envelope = json.loads(fields["envelope"])
            if not isinstance(envelope, dict):
                raise ValueError("task queue envelope must be an object")
            fair = self._fair_marker(envelope)
        except Exception as exc:
            self._release_corrupt_fair_entry(redis_id)
            envelope = {
                "message_id": redis_id,
                "attempt": self.max_attempts,
                "payload": {},
                "submitted_at": time.time(),
            }
            self._dead_letter(
                envelope,
                safe_exception_summary(exc, "task delivery failed"),
            )
            self._ack_redis_entry(redis_id)
            return
        if fair:
            decision = self._fair_schedule(redis_id, fields["envelope"], fair)
            if decision == "deferred":
                metrics.inc("queue_fair_deferrals_total")
                return
            if decision == "invalid":
                self._release_corrupt_fair_entry(redis_id)
                error = safe_exception_summary(
                    ValueError("fair queue entry tenant mismatch"),
                    "task delivery failed",
                )
                self._dead_letter(envelope, error)
                self._ack_redis_entry(redis_id)
                return
            metrics.inc("queue_fair_grants_total")
        with self._drain_condition:
            self._redis_active_ids.add(redis_id)
        try:
            self._deliver(envelope)
            # ACK only after work completed or was safely requeued/DLQed. XDEL is
            # safe because EvoAgent owns the only consumer group for this stream;
            # retaining acknowledged rows forever would make queue depth and Redis
            # memory usage grow without bound.
            self._ack_redis_entry(redis_id)
        finally:
            with self._drain_condition:
                self._redis_active_ids.discard(redis_id)
                self._drain_condition.notify_all()

    def _ack_redis_entry(self, redis_id: str) -> None:
        with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(self.STREAM, self.GROUP, redis_id)
            pipeline.xdel(self.STREAM, redis_id)
            pipeline.hdel(self.FAIR_ADMITTED, redis_id)
            pipeline.hdel(self.FAIR_ENTRIES, redis_id)
            pipeline.execute()

    def _fair_schedule(self, redis_id: str, serialized: str, fair: dict[str, Any]) -> str:
        result = int(
            self._redis.eval(
                "local admitted=redis.call('HGET',KEYS[3],ARGV[1]); "
                "if admitted then "
                "if admitted ~= ARGV[3] then return -1 end; return 1 end; "
                "local indexed=redis.call('HGET',KEYS[2],ARGV[1]); "
                "if not indexed then return -2 end; "
                "if indexed ~= ARGV[3] then return -1 end; "
                "local waiting=tonumber(redis.call('HGET',KEYS[1],ARGV[3]) or '0'); "
                "local last=redis.call('GET',KEYS[4]); "
                "local streak=tonumber(redis.call('GET',KEYS[5]) or '0'); "
                "local weight=tonumber(ARGV[4]); "
                "if waiting > 0 and last == ARGV[3] and streak >= weight "
                "and redis.call('HLEN',KEYS[1]) > 1 then "
                "local new_id=redis.call('XADD',KEYS[6],'*','envelope',ARGV[2]); "
                "redis.call('HDEL',KEYS[2],ARGV[1]); "
                "redis.call('HSET',KEYS[2],new_id,ARGV[3]); "
                "redis.call('XACK',KEYS[6],ARGV[5],ARGV[1]); "
                "redis.call('XDEL',KEYS[6],ARGV[1]); return 0 end; "
                "if waiting > 0 then "
                "local remaining=redis.call('HINCRBY',KEYS[1],ARGV[3],-1); "
                "if remaining <= 0 then redis.call('HDEL',KEYS[1],ARGV[3]) end end; "
                "redis.call('HDEL',KEYS[2],ARGV[1]); "
                "redis.call('HSET',KEYS[3],ARGV[1],ARGV[3]); "
                "if last == ARGV[3] then redis.call('INCR',KEYS[5]) else "
                "redis.call('SET',KEYS[4],ARGV[3]); redis.call('SET',KEYS[5],1) end; "
                "return 1",
                6,
                self.FAIR_WAITING,
                self.FAIR_ENTRIES,
                self.FAIR_ADMITTED,
                self.FAIR_LAST_TENANT,
                self.FAIR_STREAK,
                self.STREAM,
                redis_id,
                serialized,
                fair["tenant_key"],
                fair["weight"],
                self.GROUP,
            )
        )
        if result == 0:
            return "deferred"
        if result < 0:
            return "invalid"
        return "granted"

    def _release_corrupt_fair_entry(self, redis_id: str) -> None:
        self._redis.eval(
            "local tenant=redis.call('HGET',KEYS[2],ARGV[1]); "
            "if not tenant then return 0 end; "
            "local remaining=redis.call('HINCRBY',KEYS[1],tenant,-1); "
            "if remaining <= 0 then redis.call('HDEL',KEYS[1],tenant) end; "
            "redis.call('HDEL',KEYS[2],ARGV[1]); return 1",
            2,
            self.FAIR_WAITING,
            self.FAIR_ENTRIES,
            redis_id,
        )

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
            {
                **item,
                "error": coerce_safe_summary(item.get("error"), "task delivery failed"),
            }
            for item in values
        ]

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

    def fair_waiting_tenants(self) -> int:
        if not self._redis:
            return 0
        try:
            return int(self._redis.hlen(self.FAIR_WAITING))
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
                "fair_scheduling": False,
                "fair_policy_id": self._fair_policy.policy_id,
                "fair_waiting_tenants": 0,
                "lease_heartbeat_running": False,
            }
        running = sum(not worker.done() for worker in self._redis_workers)
        dependency_ok = False
        error = self._last_worker_error or self._last_heartbeat_error
        fair_waiting = -1
        try:
            dependency_ok = bool(self._redis.ping())
            fair_waiting = int(self._redis.hlen(self.FAIR_WAITING))
        except Exception as exc:
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
            "fair_scheduling": self._fair_scheduling,
            "fair_policy_id": self._fair_policy.policy_id,
            "fair_waiting_tenants": fair_waiting,
            "lease_heartbeat_running": heartbeat_running,
        }

    def drain(self, timeout_seconds: float = 0.0) -> bool:
        """Wait for active and already-scheduled in-memory deliveries.

        Retry timers are not kept alive during shutdown. Redis messages remain
        durable and can be reclaimed by another consumer after their lease.
        """
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._drain_condition:
            while self._active_deliveries or self._scheduled_memory or self._redis_active_ids:
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
        if drained:
            self._heartbeat_stop.set()
        if self._redis_heartbeat is not None and drained:
            self._redis_heartbeat.join(timeout=max(0.1, min(1.0, drain_timeout_seconds or 1.0)))
        self._executor.shutdown(wait=False, cancel_futures=not drained)
        if self._redis is not None and drained:
            # Closing the pool interrupts a worker blocked in XREADGROUP; its
            # reconnect loop observes `_stop` and exits without a leaked socket.
            self._redis.close()
        return drained
