"""Offline queue reconstruction after database and Redis disaster recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from .migrations import SchemaMigrationError
from .ports import RecoveryStorePort
from .postgres_store import PostgresTaskStore
from .task_queue import build_queue_keyspace, validate_redis_cluster_url

RECOVERY_SCHEMA_VERSION = 1
RECOVERY_MARKER = "evoagent:recovery:epoch"
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


class QueueRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueueRecoveryPlan:
    plan_sha256: str
    candidates: tuple[dict[str, Any], ...]
    recoverable: int
    unrecoverable: int
    by_outbox_status: dict[str, int]
    unrecoverable_task_ids: tuple[str, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _valid_recovery_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("recovery id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("recovery id must be a lowercase canonical UUID")
    return canonical


def _valid_plan_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("expected plan SHA-256 must be 64 lowercase hexadecimal characters")
    return value


def build_queue_recovery_plan(store: RecoveryStorePort, max_tasks: int) -> QueueRecoveryPlan:
    if not 0 < max_tasks <= 100_000:
        raise ValueError("max tasks must be between 1 and 100000")
    candidates = store.queue_recovery_candidates(max_tasks + 1)
    if len(candidates) > max_tasks:
        raise QueueRecoveryError(
            "recovery candidate count exceeds the configured safety limit of %d" % max_tasks
        )
    statuses: dict[str, int] = {}
    plan_items = []
    unrecoverable_ids = []
    recoverable = 0
    for candidate in candidates:
        status = str(candidate.get("outbox_status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
        payload = candidate.get("payload")
        payload_sha256 = (
            hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
            if isinstance(payload, dict)
            else ""
        )
        is_recoverable = bool(candidate.get("recoverable")) and bool(payload_sha256)
        if is_recoverable:
            recoverable += 1
        else:
            unrecoverable_ids.append(str(candidate.get("task_id", "")))
        plan_items.append(
            {
                "task_id": str(candidate.get("task_id", "")),
                "tenant_id": str(candidate.get("tenant_id", "")),
                "outbox_status": status,
                "recoverable": is_recoverable,
                "payload_sha256": payload_sha256,
            }
        )
    plan_sha256 = hashlib.sha256(_canonical_json(plan_items).encode("utf-8")).hexdigest()
    return QueueRecoveryPlan(
        plan_sha256,
        tuple(candidates),
        recoverable,
        len(unrecoverable_ids),
        statuses,
        tuple(unrecoverable_ids),
    )


class RedisRecoveryTarget:
    """An empty legacy DB or empty v2 queue namespace reserved for recovery."""

    def __init__(self, url: str, redis_cluster: bool = False, namespace: str = ""):
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"redis", "rediss"} or not host:
            raise ValueError("Redis recovery URL must use redis:// or rediss://")
        if parsed.scheme == "redis" and host not in _LOOPBACK:
            raise ValueError("Redis recovery requires TLS outside loopback")
        if parsed.fragment:
            raise ValueError("Redis recovery URL must not contain a fragment")
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise QueueRecoveryError("queue recovery requires redis") from exc
        if redis_cluster and not namespace:
            raise ValueError("Redis Cluster recovery requires EVOAGENT_QUEUE_NAMESPACE")
        if redis_cluster:
            validate_redis_cluster_url(url)
        self.redis_cluster = bool(redis_cluster)
        self.keyspace = build_queue_keyspace(namespace)
        client_type = redis.RedisCluster if self.redis_cluster else redis.Redis
        self._client: Any = client_type.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )
        self._client.ping()

    def inspect(self, marker: str) -> str:
        if self.keyspace.version == 2:
            marker_key = self.keyspace.recovery_marker
            occupied = int(self._client.exists(marker_key, *self.keyspace.fixed_keys))
            if occupied == 0:
                return "empty"
            if occupied == 1 and self._client.get(marker_key) == marker:
                return "reserved"
            return "nonempty"
        size = int(self._client.dbsize())
        if size == 0:
            return "empty"
        if size == 1 and self._client.get(RECOVERY_MARKER) == marker:
            return "reserved"
        return "nonempty"

    def reserve(self, marker: str) -> str:
        if self.keyspace.version == 2:
            result = self._client.eval(
                "for index=2,#KEYS do "
                "if redis.call('EXISTS',KEYS[index]) == 1 then return 'nonempty' end end; "
                "local current=redis.call('GET',KEYS[1]); "
                "if not current then redis.call('SET',KEYS[1],ARGV[1]); return 'reserved' end; "
                "if current==ARGV[1] then return 'existing' end; return 'nonempty'",
                1 + len(self.keyspace.fixed_keys),
                self.keyspace.recovery_marker,
                *self.keyspace.fixed_keys,
                marker,
            )
            return str(result)
        result = self._client.eval(
            "local size=redis.call('DBSIZE'); "
            "if size==0 then redis.call('SET',KEYS[1],ARGV[1]); return 'reserved' end; "
            "if size==1 and redis.call('GET',KEYS[1])==ARGV[1] then return 'existing' end; "
            "return 'nonempty'",
            1,
            RECOVERY_MARKER,
            marker,
        )
        return str(result)

    def close(self) -> None:
        self._client.close()


def _marker(recovery_id: str, database: str, plan_sha256: str) -> str:
    return _canonical_json(
        {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "recovery_id": recovery_id,
            "database": database,
            "plan_sha256": plan_sha256,
        }
    )


def _target_evidence(redis_target: Any) -> dict[str, Any]:
    keyspace = getattr(redis_target, "keyspace", None)
    return {
        "redis_cluster": bool(getattr(redis_target, "redis_cluster", False)),
        "queue_namespace": str(getattr(keyspace, "namespace", "")),
        "keyspace_version": int(getattr(keyspace, "version", 1)),
    }


def execute_queue_recovery(
    store: RecoveryStorePort,
    redis_target: Any,
    recovery_id: str,
    database: str,
    max_tasks: int = 10_000,
    apply: bool = False,
    allow_unrecoverable: bool = False,
    expected_plan_sha256: str | None = None,
) -> dict[str, Any]:
    recovery_id = _valid_recovery_id(recovery_id)
    if not database:
        raise ValueError("confirmed database name is required")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _valid_plan_sha256(expected_plan_sha256)
    if apply and expected_plan_sha256 is None:
        raise ValueError("apply requires the SHA-256 from a reviewed dry-run plan")

    # The first successful staging mutates Outbox state. Use the immutable audit
    # record for retries, otherwise a formerly missing Outbox row would produce a
    # different live-plan hash and make the committed epoch impossible to retry.
    existing = store.get_queue_recovery(recovery_id)
    if existing:
        recorded_sha256 = _valid_plan_sha256(str(existing.get("plan_sha256", "")))
        if expected_plan_sha256 is not None and expected_plan_sha256 != recorded_sha256:
            raise QueueRecoveryError("expected plan SHA-256 does not match the applied epoch")
        target_state = str(redis_target.inspect(_marker(recovery_id, database, recorded_sha256)))
        if target_state == "empty":
            raise QueueRecoveryError(
                "recovery id was already applied to a different Redis target; generate a new UUID"
            )
        if target_state == "nonempty":
            raise QueueRecoveryError(
                "Redis recovery database is not empty or reserved for this applied epoch"
            )
        staging = {key: value for key, value in existing.items() if key != "created_at"}
        staging["already_applied"] = True
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "pass",
            "applied": True,
            "eligible": True,
            "already_applied": True,
            "recovery_id": recovery_id,
            "database": database,
            "redis_target_state": "reserved",
            "redis_topology": _target_evidence(redis_target),
            "reservation": "existing",
            "plan": {
                "plan_sha256": recorded_sha256,
                "candidate_count": existing.get("candidate_count", 0),
            },
            "staging": staging,
            "next_action": "start Outbox dispatcher, then workers, then external effects and traffic",
        }

    plan = build_queue_recovery_plan(store, max_tasks)
    if expected_plan_sha256 is not None and expected_plan_sha256 != plan.plan_sha256:
        raise QueueRecoveryError("expected plan SHA-256 does not match the current recovery plan")
    marker = _marker(recovery_id, database, plan.plan_sha256)
    target_state = str(redis_target.inspect(marker))
    eligible = target_state in {"empty", "reserved"}
    if target_state == "nonempty":
        raise QueueRecoveryError("Redis recovery database is not empty or reserved for this plan")
    if plan.unrecoverable and not allow_unrecoverable:
        raise QueueRecoveryError(
            "%d incomplete tasks do not have a valid Outbox payload or stored Diff"
            % plan.unrecoverable
        )
    public_plan = {
        "plan_sha256": plan.plan_sha256,
        "candidate_count": len(plan.candidates),
        "recoverable": plan.recoverable,
        "unrecoverable": plan.unrecoverable,
        "by_outbox_status": plan.by_outbox_status,
        "unrecoverable_task_ids": list(plan.unrecoverable_task_ids[:100]),
        "unrecoverable_ids_truncated": len(plan.unrecoverable_task_ids) > 100,
    }
    if not apply:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "planned",
            "applied": False,
            "eligible": eligible,
            "recovery_id": recovery_id,
            "database": database,
            "redis_target_state": target_state,
            "redis_topology": _target_evidence(redis_target),
            "plan": public_plan,
        }
    reservation = str(redis_target.reserve(marker))
    if reservation not in {"reserved", "existing"}:
        raise QueueRecoveryError("Redis recovery epoch could not be reserved atomically")
    staged = store.stage_queue_recovery(
        recovery_id,
        plan.plan_sha256,
        list(plan.candidates),
    )
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "status": "pass",
        "applied": True,
        "eligible": True,
        "recovery_id": recovery_id,
        "database": database,
        "redis_target_state": "reserved",
        "redis_topology": _target_evidence(redis_target),
        "reservation": reservation,
        "plan": public_plan,
        "staging": staged,
        "next_action": "start Outbox dispatcher, then workers, then external effects and traffic",
    }


def _database_name(url: str) -> str:
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise QueueRecoveryError("queue recovery requires psycopg") from exc
    try:
        database = str(conninfo_to_dict(url).get("dbname", ""))
    except Exception as exc:
        raise ValueError("invalid PostgreSQL connection string") from exc
    if not database:
        raise ValueError("PostgreSQL connection string must name a database")
    return database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage incomplete EvoAgent task intents for an empty Redis recovery target"
    )
    parser.add_argument("--recovery-id", required=True)
    parser.add_argument("--confirm-database", required=True)
    parser.add_argument("--max-tasks", type=int, default=10_000)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--allow-unrecoverable", action="store_true")
    args = parser.parse_args(argv)
    database_url = os.getenv("EVOAGENT_DATABASE_URL", "")
    redis_url = os.getenv("EVOAGENT_REDIS_URL", "")
    redis_cluster = os.getenv("EVOAGENT_REDIS_CLUSTER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    queue_namespace = os.getenv("EVOAGENT_QUEUE_NAMESPACE", "")
    store = None
    target = None
    try:
        if not database_url or not redis_url:
            raise ValueError(
                "EVOAGENT_DATABASE_URL and EVOAGENT_REDIS_URL are required for queue recovery"
            )
        database = _database_name(database_url)
        if args.confirm_database != database:
            raise ValueError("--confirm-database does not match EVOAGENT_DATABASE_URL")
        store = PostgresTaskStore(
            database_url,
            pool_min=0,
            pool_max=0,
            pool_timeout=5,
            auto_migrate=False,
        )
        if store.connected_database_name() != database:
            raise ValueError("connected PostgreSQL database does not match the confirmed name")
        target = RedisRecoveryTarget(redis_url, redis_cluster, queue_namespace)
        report = execute_queue_recovery(
            store,
            target,
            args.recovery_id,
            database,
            args.max_tasks,
            args.apply,
            args.allow_unrecoverable,
            args.expect_plan_sha256,
        )
    except (ValueError, OSError, QueueRecoveryError, SchemaMigrationError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": RECOVERY_SCHEMA_VERSION,
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # pragma: no cover - external driver boundary
        print(
            json.dumps(
                {
                    "schema_version": RECOVERY_SCHEMA_VERSION,
                    "status": "error",
                    "error": "queue recovery failed (%s)" % type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if target is not None:
            target.close()
        if store is not None:
            store.close()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
