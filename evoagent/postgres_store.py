"""PostgreSQL persistence backend.

The implementation mirrors TaskStore's public API and is selected when
EVOAGENT_DATABASE_URL starts with postgres. The driver and bounded connection
pool are runtime dependencies; local development remains zero-config because
SQLite is selected when no database URL is supplied.
"""

import json
import uuid
from contextlib import AbstractContextManager
from typing import Any

from .errors import preserve_safe_summary, safe_exception_summary
from .migrations import migrate_postgres, validate_current_schema_history
from .models import ReviewReport, TaskState, TraceEvent
from .ports import ApplicationStorePort
from .store import utc_after, utc_now


class PostgresTaskStore:
    def __init__(
        self,
        url: str,
        pool_min: int = 1,
        pool_max: int = 10,
        pool_timeout: float = 10.0,
        auto_migrate: bool = True,
    ):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL mode requires: pip install psycopg[binary]") from exc
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.url = url
        self.pool_timeout = float(pool_timeout)
        self.auto_migrate = bool(auto_migrate)
        # A real connection pool avoids a TCP connect + auth handshake on every
        # single query (the previous per-call `psycopg.connect` was the dominant
        # Postgres cost under load). Keep the import guard so a deliberately
        # stripped/embedder installation fails visibly but can still connect.
        self._pool = None
        if pool_max and pool_max > 0:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                print(
                    "WARNING: psycopg_pool not installed; falling back to a new "
                    "connection per query. Reinstall EvoAgent with its declared "
                    "runtime dependencies to enable bounded pooling."
                )
            else:
                try:
                    # open=False + explicit open() avoids the deprecated eager
                    # constructor-open path in psycopg_pool >= 3.2.
                    self._pool = ConnectionPool(
                        conninfo=url,
                        min_size=min(max(0, pool_min), pool_max),
                        max_size=pool_max,
                        timeout=self.pool_timeout,
                        kwargs={"row_factory": dict_row},
                        open=False,
                    )
                    self._pool.open()
                except Exception as exc:
                    print(
                        "WARNING: could not create Postgres pool (%s); using per-call connections"
                        % safe_exception_summary(exc, "store readiness failed")
                    )
                    self._pool = None
        try:
            self._init()
        except Exception:
            # Do not leak the pool's background threads/connections if schema
            # initialization fails (e.g. Postgres briefly unavailable at boot).
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            raise

    def _connect(self) -> AbstractContextManager[Any]:
        """Return a context manager yielding a connection. With a pool the
        connection is checked out and returned on ``__exit__``; without one a
        fresh connection is created and closed (psycopg3 semantics)."""
        if self._pool is not None:
            return self._pool.connection(timeout=self.pool_timeout)
        return self.psycopg.connect(self.url, row_factory=self.dict_row)

    def has_pool(self) -> bool:
        return self._pool is not None

    def ping(self) -> None:
        """Lightweight readiness probe: confirm the database is reachable."""
        with self._connect() as conn:
            conn.execute("SELECT 1")

    def connected_database_name(self) -> str:
        """Return the server-reported database, not merely the DSN value."""
        with self._connect() as conn:
            row = conn.execute("SELECT current_database() AS database_name").fetchone()
        return str(row["database_name"])

    def pool_stats(self) -> dict[str, int] | None:
        """psycopg_pool stats for metrics, or ``None`` when unpooled."""
        if self._pool is None:
            return None
        try:
            return dict(self._pool.get_stats())
        except Exception:  # pragma: no cover - defensive
            return None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def _init(self) -> None:
        with self._connect() as conn:
            if self.auto_migrate:
                self._schema_version = migrate_postgres(conn)
            else:
                rows = conn.execute(
                    "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                self._schema_version = validate_current_schema_history(list(rows))

    def schema_version(self) -> int:
        return self._schema_version

    def create(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        payload: dict[str, Any],
        tenant_id: str = "default",
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,error,"
                "created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,NULL,NULL,%s,%s,%s,FALSE)",
                (
                    task_id,
                    TaskState.PENDING.value,
                    repository,
                    pull_request,
                    json.dumps(payload),
                    now,
                    now,
                    tenant_id,
                ),
            )

    def create_review_task(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        payload: dict[str, Any],
        tenant_id: str,
        diff: str | None = None,
        outbox_payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist task, optional diff, and queue intent in one transaction."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,error,"
                "created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,NULL,NULL,%s,%s,%s,FALSE)",
                (
                    task_id,
                    TaskState.PENDING.value,
                    repository,
                    pull_request,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                    tenant_id,
                ),
            )
            if diff is not None:
                conn.execute(
                    "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (%s,%s,%s)",
                    (task_id, diff, now),
                )
            if outbox_payload is not None:
                conn.execute(
                    "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                    "attempts,available_at,created_at,updated_at) "
                    "VALUES (%s,%s,%s,%s::jsonb,'pending',0,%s,%s,%s)",
                    (
                        "review:" + task_id,
                        "review",
                        task_id,
                        json.dumps(outbox_payload, ensure_ascii=False),
                        now,
                        now,
                        now,
                    ),
                )

    def claim_outbox(
        self,
        owner: str,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> list[dict[str, Any]]:
        now = utc_now()
        lease_until = utc_after(lease_seconds)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_messages WHERE attempts < %s AND "
                "((status='pending' AND available_at<=%s) OR "
                "(status='publishing' AND lease_until<%s)) "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s",
                (max_attempts, now, now, max(1, min(limit, 500))),
            ).fetchall()
            claimed = []
            for row in rows:
                conn.execute(
                    "UPDATE outbox_messages SET status='publishing',attempts=attempts+1,"
                    "lease_owner=%s,lease_until=%s,updated_at=%s WHERE id=%s",
                    (owner, lease_until, now, row["id"]),
                )
                item = dict(row)
                item["attempts"] = int(item["attempts"]) + 1
                raw_payload = item.pop("payload_json")
                item["payload"] = (
                    json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
                )
                claimed.append(item)
        return claimed

    def mark_outbox_published(self, message_id: str, owner: str) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE outbox_messages SET status='published',published_at=%s,updated_at=%s,"
                "lease_owner=NULL,lease_until=NULL,last_error=NULL "
                "WHERE id=%s AND status='publishing' AND lease_owner=%s",
                (now, now, message_id, owner),
            )
        return cursor.rowcount > 0

    def release_outbox(
        self,
        message_id: str,
        owner: str,
        error: str,
        retry_delay_seconds: float,
        max_attempts: int,
    ) -> bool:
        now = utc_now()
        available_at = utc_after(retry_delay_seconds)
        error = preserve_safe_summary(error, "outbox dispatch failed")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE outbox_messages SET "
                "status=CASE WHEN attempts>=%s THEN 'dead' ELSE 'pending' END,"
                "available_at=%s,lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=%s "
                "WHERE id=%s AND status='publishing' AND lease_owner=%s",
                (max_attempts, available_at, error[:2000], now, message_id, owner),
            )
        return cursor.rowcount > 0

    def outbox_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total,"
                "COUNT(*) FILTER(WHERE status='pending') AS pending,"
                "COUNT(*) FILTER(WHERE status='publishing') AS publishing,"
                "COUNT(*) FILTER(WHERE status='dead') AS dead,"
                "EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - "
                "MIN(created_at) FILTER(WHERE status IN ('pending','publishing')))) "
                "AS oldest_age_seconds FROM outbox_messages"
            ).fetchone()
        result: dict[str, Any] = {
            key: int(row[key] or 0) for key in ("total", "pending", "publishing", "dead")
        }
        result["oldest_age_seconds"] = float(row["oldest_age_seconds"] or 0.0)
        return result

    def list_outbox(self, status: str = "dead", limit: int = 100) -> list:
        if status not in {"pending", "publishing", "published", "dead"}:
            raise ValueError("unsupported outbox status")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox_messages WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                (status, max(1, min(limit, 500))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            raw_payload = item.pop("payload_json")
            item["payload"] = (
                json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            )
            values.append(item)
        return values

    def requeue_outbox(self, message_id: str) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE outbox_messages SET status='pending',attempts=0,available_at=%s,"
                "lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=%s "
                "WHERE id=%s AND status='dead'",
                (now, now, message_id),
            )
        return cursor.rowcount > 0

    def queue_recovery_candidates(self, limit: int) -> list[dict[str, Any]]:
        """Describe non-terminal task intents without mutating queue state."""
        bounded = max(1, min(int(limit), 100_001))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.id,t.repository,t.pull_request,t.tenant_id,o.status AS outbox_status,"
                "o.payload_json,(p.task_id IS NOT NULL) AS has_payload "
                "FROM tasks t LEFT JOIN outbox_messages o ON o.id='review:'||t.id "
                "LEFT JOIN task_payloads p ON p.task_id=t.id "
                "WHERE t.state NOT IN (%s,%s,%s) AND t.cancel_requested=FALSE "
                "ORDER BY t.created_at,t.id LIMIT %s",
                (
                    TaskState.SUCCESS.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                    bounded,
                ),
            ).fetchall()
        candidates = []
        for row in rows:
            raw_payload = row["payload_json"]
            payload = (
                json.loads(raw_payload)
                if isinstance(raw_payload, str)
                else raw_payload
                if raw_payload is not None
                else {
                    "task_id": row["id"],
                    "repository": row["repository"],
                    "pull_request": row["pull_request"],
                    "tenant_id": row["tenant_id"],
                }
                if row["has_payload"]
                else None
            )
            if isinstance(payload, dict):
                payload = {
                    **payload,
                    "task_id": row["id"],
                    "repository": row["repository"],
                    "pull_request": row["pull_request"],
                    "tenant_id": row["tenant_id"],
                }
            recoverable = isinstance(payload, dict)
            candidates.append(
                {
                    "task_id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "outbox_status": row["outbox_status"] or "missing",
                    "payload": payload,
                    "recoverable": recoverable,
                    "reason": "" if recoverable else "valid recovery payload is missing",
                }
            )
        return candidates

    def get_queue_recovery(self, recovery_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail_json,created_at FROM audit_log "
                "WHERE tenant_id='system' AND action='recovery.queue.stage' AND resource=%s "
                "ORDER BY id DESC LIMIT 1",
                (recovery_id,),
            ).fetchone()
        if not row:
            return None
        detail = row["detail_json"]
        return {
            **(json.loads(detail) if isinstance(detail, str) else detail),
            "created_at": row["created_at"],
        }

    def stage_queue_recovery(
        self,
        recovery_id: str,
        plan_sha256: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", ("queue-recovery:" + recovery_id,)
            )
            existing = conn.execute(
                "SELECT detail_json FROM audit_log WHERE tenant_id='system' "
                "AND action='recovery.queue.stage' AND resource=%s ORDER BY id DESC LIMIT 1",
                (recovery_id,),
            ).fetchone()
            if existing:
                raw_detail = existing["detail_json"]
                detail = json.loads(raw_detail) if isinstance(raw_detail, str) else raw_detail
                if detail.get("plan_sha256") != plan_sha256:
                    raise ValueError("recovery id was already used for a different plan")
                return {**detail, "already_applied": True}
            staged = 0
            skipped_terminal = 0
            skipped_unrecoverable = 0
            preserved_outbox_history = 0
            tenants = set()
            for candidate in candidates:
                task = conn.execute(
                    "SELECT state,cancel_requested,tenant_id FROM tasks WHERE id=%s FOR UPDATE",
                    (candidate["task_id"],),
                ).fetchone()
                if (
                    not task
                    or task["state"]
                    in {
                        TaskState.SUCCESS.value,
                        TaskState.FAILED.value,
                        TaskState.CANCELLED.value,
                    }
                    or bool(task["cancel_requested"])
                ):
                    skipped_terminal += 1
                    continue
                payload = candidate.get("payload")
                if not isinstance(payload, dict):
                    skipped_unrecoverable += 1
                    continue
                message_id = "review:" + candidate["task_id"]
                serialized = json.dumps(payload, ensure_ascii=False)
                outbox = conn.execute(
                    "SELECT status FROM outbox_messages WHERE id=%s FOR UPDATE", (message_id,)
                ).fetchone()
                if outbox and outbox["status"] in {"published", "dead"}:
                    recovery_message_id = "recovery:%s:%s" % (
                        recovery_id,
                        candidate["task_id"],
                    )
                    conn.execute(
                        "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                        "attempts,available_at,created_at,updated_at) "
                        "VALUES (%s,'review',%s,%s::jsonb,'pending',0,%s,%s,%s)",
                        (
                            recovery_message_id,
                            "%s:%s" % (recovery_id, candidate["task_id"]),
                            serialized,
                            now,
                            now,
                            now,
                        ),
                    )
                    preserved_outbox_history += 1
                elif outbox:
                    conn.execute(
                        "UPDATE outbox_messages SET payload_json=%s::jsonb,status='pending',"
                        "attempts=0,available_at=%s,lease_owner=NULL,lease_until=NULL,"
                        "last_error=NULL,updated_at=%s WHERE id=%s",
                        (serialized, now, now, message_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                        "attempts,available_at,created_at,updated_at) "
                        "VALUES (%s,'review',%s,%s::jsonb,'pending',0,%s,%s,%s)",
                        (message_id, candidate["task_id"], serialized, now, now, now),
                    )
                staged += 1
                tenants.add(str(task["tenant_id"]))
            detail = {
                "recovery_id": recovery_id,
                "plan_sha256": plan_sha256,
                "candidate_count": len(candidates),
                "staged": staged,
                "skipped_terminal": skipped_terminal,
                "skipped_unrecoverable": skipped_unrecoverable,
                "preserved_outbox_history": preserved_outbox_history,
                "tenant_count": len(tenants),
            }
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES ('system','evoagent-recover','recovery.queue.stage',%s,%s::jsonb,%s)",
                (recovery_id, json.dumps(detail, ensure_ascii=False), now),
            )
        return {**detail, "already_applied": False}

    def reserve_model_usage(
        self,
        record: dict[str, Any],
        period_start: str,
        token_budget: int = 0,
        cost_budget_micros: int = 0,
        lane_token_budget: int = 0,
        lane_cost_budget_micros: int = 0,
    ) -> bool:
        """Atomically enforce one repository's period budget and reserve capacity."""
        if record.get("lane", "active") not in {"active", "shadow"}:
            raise ValueError("model usage lane must be active or shadow")
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (
                    "model-budget:%s:%s:%s"
                    % (record["tenant_id"], record["repository"], period_start[:10]),
                ),
            )
            used = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') "
                "THEN reserved_tokens "
                "ELSE input_tokens+output_tokens END),0) AS tokens,"
                "COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') "
                "THEN reserved_cost_micros "
                "ELSE cost_micros END),0) AS cost FROM model_usage "
                "WHERE tenant_id=%s AND repository=%s AND created_at>=%s "
                "AND status IN ('reserved','uncertain','success','failed')",
                (record["tenant_id"], record["repository"], period_start),
            ).fetchone()
            if (
                token_budget > 0
                and int(used["tokens"]) + int(record["reserved_tokens"]) > token_budget
            ):
                return False
            if (
                cost_budget_micros > 0
                and int(used["cost"]) + int(record["reserved_cost_micros"]) > cost_budget_micros
            ):
                return False
            lane = record.get("lane", "active")
            if lane_token_budget > 0 or lane_cost_budget_micros > 0:
                lane_used = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') "
                    "THEN reserved_tokens ELSE input_tokens+output_tokens END),0) AS tokens,"
                    "COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') "
                    "THEN reserved_cost_micros ELSE cost_micros END),0) AS cost "
                    "FROM model_usage WHERE tenant_id=%s AND repository=%s AND created_at>=%s "
                    "AND lane=%s AND status IN ('reserved','uncertain','success','failed')",
                    (record["tenant_id"], record["repository"], period_start, lane),
                ).fetchone()
                if (
                    lane_token_budget > 0
                    and int(lane_used["tokens"]) + int(record["reserved_tokens"])
                    > lane_token_budget
                ):
                    return False
                if (
                    lane_cost_budget_micros > 0
                    and int(lane_used["cost"]) + int(record["reserved_cost_micros"])
                    > lane_cost_budget_micros
                ):
                    return False
            conn.execute(
                "INSERT INTO model_usage(request_id,root_request_id,route_id,attempt,tenant_id,"
                "repository,task_id,purpose,provider,model,status,reserved_tokens,"
                "reserved_cost_micros,redactions,request_sha256,lane,topology_sha256,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s,%s,%s,%s)",
                (
                    record["request_id"],
                    record.get("root_request_id") or record["request_id"],
                    record.get("route_id") or "%s-%s" % (record["provider"], record["model"]),
                    record.get("attempt", 1),
                    record["tenant_id"],
                    record["repository"],
                    record.get("task_id"),
                    record["purpose"],
                    record["provider"],
                    record["model"],
                    record["reserved_tokens"],
                    record.get("reserved_cost_micros", 0),
                    record.get("redactions", 0),
                    record["request_sha256"],
                    record.get("lane", "active"),
                    record.get("topology_sha256") or None,
                    record.get("created_at") or utc_now(),
                ),
            )
        return True

    def complete_model_usage(
        self,
        request_id: str,
        status: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        error: str = "",
    ) -> bool:
        if status not in {"success", "failed"}:
            raise ValueError("model usage status must be success or failed")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE model_usage SET status=%s,input_tokens=%s,output_tokens=%s,"
                "cost_micros=%s,error=%s,completed_at=%s "
                "WHERE request_id=%s AND status IN ('reserved','uncertain')",
                (
                    status,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, cost_micros),
                    error[:2000] or None,
                    utc_now(),
                    request_id,
                ),
            )
        return cursor.rowcount > 0

    def expire_model_usage_reservations(self, cutoff: str, limit: int = 1000) -> int:
        """Quarantine expired in-flight calls without releasing unknown provider cost."""
        bounded_limit = max(1, min(limit, 10_000))
        with self._connect() as conn:
            rows = conn.execute(
                "WITH candidates AS ("
                "SELECT request_id FROM model_usage "
                "WHERE status='reserved' AND created_at<%s "
                "ORDER BY created_at,request_id LIMIT %s FOR UPDATE SKIP LOCKED"
                ") UPDATE model_usage AS usage SET status='uncertain',"
                "error='reservation expired before durable completion; reconciliation required' "
                "FROM candidates WHERE usage.request_id=candidates.request_id "
                "AND usage.status='reserved' RETURNING usage.request_id",
                (cutoff, bounded_limit),
            ).fetchall()
        return len(rows)

    def reconcile_model_usage(
        self,
        tenant_id: str,
        actor: str,
        request_id: str,
        status: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        error: str = "",
    ) -> bool:
        """Apply operator-verified provider usage to one uncertain reservation."""
        if status not in {"success", "failed"}:
            raise ValueError("model usage status must be success or failed")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE model_usage SET status=%s,input_tokens=%s,output_tokens=%s,"
                "cost_micros=%s,error=%s,completed_at=%s "
                "WHERE tenant_id=%s AND request_id=%s AND status='uncertain'",
                (
                    status,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, cost_micros),
                    error[:2000] or None,
                    utc_now(),
                    tenant_id,
                    request_id,
                ),
            )
            if cursor.rowcount:
                detail = {
                    "status": status,
                    "input_tokens": max(0, input_tokens),
                    "output_tokens": max(0, output_tokens),
                    "cost_micros": max(0, cost_micros),
                }
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                    (
                        tenant_id,
                        actor,
                        "model-usage.reconciled",
                        request_id,
                        json.dumps(detail, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        return cursor.rowcount > 0

    def list_model_usage(
        self, tenant_id: str, repository: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM model_usage WHERE tenant_id=%s"
        params: list[Any] = [tenant_id]
        if repository is not None:
            query += " AND repository=%s"
            params.append(repository)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(1, min(limit, 1000)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            for key in ("created_at", "completed_at"):
                if item.get(key) is not None:
                    item[key] = item[key].isoformat()
            values.append(item)
        return values

    def start_model_route_shadow(self, record: dict[str, Any]) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO model_route_shadows(observation_id,topology_sha256,root_request_id,"
                "tenant_id,repository,task_id,purpose,active_route_id,candidate_route_id,status,"
                "active_output_sha256,input_sha256,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'scheduled',%s,%s,%s) "
                "ON CONFLICT(observation_id) DO NOTHING RETURNING observation_id",
                (
                    record["observation_id"],
                    record["topology_sha256"],
                    record["root_request_id"],
                    record["tenant_id"],
                    record["repository"],
                    record.get("task_id"),
                    record["purpose"],
                    record["active_route_id"],
                    record["candidate_route_id"],
                    record["active_output_sha256"],
                    record["input_sha256"],
                    record.get("created_at") or utc_now(),
                ),
            ).fetchone()
        return row is not None

    def complete_model_route_shadow(
        self,
        observation_id: str,
        status: str,
        agreement: bool | None,
        candidate_output_sha256: str,
        input_tokens: int,
        output_tokens: int,
        cost_micros: int,
        duration_ms: int,
        error_type: str = "",
        error_ref: str = "",
    ) -> bool:
        if status not in {"success", "failed", "budget-rejected", "shed", "cancelled"}:
            raise ValueError("invalid model route shadow status")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE model_route_shadows SET status=%s,agreement=%s,"
                "candidate_output_sha256=%s,input_tokens=%s,output_tokens=%s,cost_micros=%s,"
                "duration_ms=%s,error_type=%s,error_ref=%s,completed_at=%s "
                "WHERE observation_id=%s AND status='scheduled'",
                (
                    status,
                    agreement,
                    candidate_output_sha256 or None,
                    max(0, input_tokens),
                    max(0, output_tokens),
                    max(0, cost_micros),
                    max(0, duration_ms),
                    error_type[:160] or None,
                    error_ref[:16] or None,
                    utc_now(),
                    observation_id,
                ),
            )
        return cursor.rowcount > 0

    def expire_model_route_shadows(self, cutoff: str, limit: int = 1000) -> int:
        """Make crash-orphaned scheduled observations a terminal gate failure."""
        bounded_limit = max(1, min(limit, 10_000))
        with self._connect() as conn:
            rows = conn.execute(
                "WITH candidates AS (SELECT observation_id FROM model_route_shadows "
                "WHERE status='scheduled' AND created_at<%s "
                "ORDER BY created_at,observation_id LIMIT %s FOR UPDATE SKIP LOCKED) "
                "UPDATE model_route_shadows AS shadow SET status='uncertain',"
                "error_type='evoagent.shadow.ObservationExpired',"
                "error_ref='0000000000000000',completed_at=%s FROM candidates "
                "WHERE shadow.observation_id=candidates.observation_id "
                "AND shadow.status='scheduled' RETURNING shadow.observation_id",
                (cutoff, bounded_limit, utc_now()),
            ).fetchall()
        return len(rows)

    def model_route_shadow_stats(
        self,
        tenant_id: str,
        candidate_route_id: str,
        topology_sha256: str,
        repository: str | None = None,
    ) -> dict[str, int]:
        query = (
            "SELECT COUNT(*) AS attempts,"
            "COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS samples,"
            "COALESCE(SUM(CASE WHEN status NOT IN ('scheduled','success') THEN 1 ELSE 0 END),0) "
            "AS errors,"
            "COALESCE(SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END),0) AS pending,"
            "COALESCE(SUM(CASE WHEN status='success' AND agreement=FALSE THEN 1 ELSE 0 END),0) "
            "AS disagreements,COALESCE(SUM(input_tokens),0) AS input_tokens,"
            "COALESCE(SUM(output_tokens),0) AS output_tokens,"
            "COALESCE(SUM(cost_micros),0) AS cost_micros "
            "FROM model_route_shadows WHERE tenant_id=%s AND candidate_route_id=%s "
            "AND topology_sha256=%s"
        )
        params: list[Any] = [tenant_id, candidate_route_id, topology_sha256]
        if repository is not None:
            query += " AND repository=%s"
            params.append(repository)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def claim_effect(self, effect_key: str, owner: str, lease_seconds: float) -> dict[str, Any]:
        now = utc_now()
        lease_until = utc_after(lease_seconds)
        with self._connect() as conn:
            inserted = conn.execute(
                "INSERT INTO effect_receipts(effect_key,status,owner,lease_until,attempts,"
                "created_at,updated_at) VALUES (%s,'in-progress',%s,%s,1,%s,%s) "
                "ON CONFLICT(effect_key) DO NOTHING RETURNING effect_key",
                (effect_key, owner, lease_until, now, now),
            ).fetchone()
            if inserted:
                return {"status": "acquired"}
            row = conn.execute(
                "SELECT * FROM effect_receipts WHERE effect_key=%s FOR UPDATE", (effect_key,)
            ).fetchone()
            if row["status"] == "completed":
                raw_result = row["result_json"]
                return {
                    "status": "completed",
                    "result": json.loads(raw_result) if isinstance(raw_result, str) else raw_result,
                }
            acquired = conn.execute(
                "UPDATE effect_receipts SET owner=%s,lease_until=%s,attempts=attempts+1,"
                "last_error=NULL,updated_at=%s WHERE effect_key=%s AND lease_until<%s "
                "RETURNING effect_key",
                (owner, lease_until, now, effect_key, now),
            ).fetchone()
            if acquired:
                return {"status": "acquired"}
        return {"status": "busy"}

    def complete_effect(self, effect_key: str, owner: str, result: dict[str, Any]) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE effect_receipts SET status='completed',result_json=%s::jsonb,owner=NULL,"
                "lease_until=NULL,last_error=NULL,updated_at=%s,completed_at=%s "
                "WHERE effect_key=%s AND status='in-progress' AND owner=%s",
                (json.dumps(result, ensure_ascii=False), now, now, effect_key, owner),
            )
        return cursor.rowcount > 0

    def release_effect(self, effect_key: str, owner: str, error: str) -> bool:
        now = utc_now()
        error = preserve_safe_summary(error, "external effect failed")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE effect_receipts SET lease_until=%s,last_error=%s,updated_at=%s "
                "WHERE effect_key=%s AND status='in-progress' AND owner=%s",
                (now, error[:2000], now, effect_key, owner),
            )
        return cursor.rowcount > 0

    def transition(self, task_id: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,updated_at=%s WHERE id=%s",
                (event.state.value, event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def succeed(self, task_id: str, report: ReviewReport, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,report_json=%s::jsonb,updated_at=%s WHERE id=%s",
                (
                    TaskState.SUCCESS.value,
                    json.dumps(report.to_dict(), ensure_ascii=False),
                    event.created_at,
                    task_id,
                ),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None:
        error = preserve_safe_summary(error, "review execution failed")
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,error=%s,updated_at=%s WHERE id=%s",
                (TaskState.FAILED.value, error[:2000], event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, error, event.created_at),
            )

    def get(self, task_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            query = "SELECT * FROM tasks WHERE id=%s"
            params = [task_id]
            if tenant_id is not None:
                query += " AND tenant_id=%s"
                params.append(tenant_id)
            row = conn.execute(query, params).fetchone()
            if not row:
                return None
            events = conn.execute(
                "SELECT step,state,message,created_at FROM trace_events WHERE task_id=%s ORDER BY id",
                (task_id,),
            ).fetchall()
            messages = conn.execute(
                "SELECT sender,recipient,kind,correlation_id,content_json,created_at "
                "FROM agent_messages WHERE task_id=%s ORDER BY id",
                (task_id,),
            ).fetchall()
        value = dict(row)
        value["input"] = value.pop("input_json")
        value["report"] = value.pop("report_json")
        value["trace"] = [dict(item) for item in events]
        value["collaboration"] = []
        for message in messages:
            item = dict(message)
            item["content"] = item.pop("content_json")
            item["created_at"] = item["created_at"].isoformat()
            value["collaboration"].append(item)
        for key in ("created_at", "updated_at"):
            value[key] = value[key].isoformat()
        for item in value["trace"]:
            item["created_at"] = item["created_at"].isoformat()
        return value

    def record_agent_message(self, task_id: str, message: dict[str, Any]) -> None:
        content = dict(message.get("content", {}))
        if message.get("kind") == "agent_failure":
            content = {"error": preserve_safe_summary(content.get("error"), "review agent failed")}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,correlation_id,"
                "content_json,created_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    task_id,
                    message["sender"],
                    message["recipient"],
                    message["kind"],
                    message.get("correlation_id", ""),
                    json.dumps(content, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def start_session_turn(
        self,
        tenant_id: str,
        repository: str,
        pull_request: int,
        head_sha: str | None,
        trigger: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            return self._start_session_turn_in_transaction(
                conn,
                tenant_id,
                repository,
                pull_request,
                head_sha,
                trigger,
                task_id,
                utc_now(),
            )

    def _start_session_turn_in_transaction(
        self,
        conn,
        tenant_id: str,
        repository: str,
        pull_request: int,
        head_sha: str | None,
        trigger: str,
        task_id: str | None,
        now: str,
    ) -> dict[str, Any]:
        # Serialize get-or-create + sequence allocation per PR so concurrent
        # opened/synchronize deliveries cannot duplicate a session or collide
        # on a turn sequence (READ COMMITTED would otherwise allow both).
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("session:%s:%s:%s" % (tenant_id, repository, pull_request),),
        )
        new_id = str(uuid.uuid4())
        inserted = conn.execute(
            "INSERT INTO review_sessions(id,tenant_id,repository,pull_request,status,"
            "latest_head_sha,created_at,updated_at) VALUES (%s,%s,%s,%s,'open',%s,%s,%s) "
            "ON CONFLICT(tenant_id,repository,pull_request) DO NOTHING RETURNING id",
            (new_id, tenant_id, repository, pull_request, head_sha, now, now),
        ).fetchone()
        is_new = inserted is not None
        row = conn.execute(
            "SELECT id, latest_head_sha FROM review_sessions "
            "WHERE tenant_id=%s AND repository=%s AND pull_request=%s",
            (tenant_id, repository, pull_request),
        ).fetchone()
        session_id = row["id"]
        previous_head = None if is_new else row["latest_head_sha"]
        sequence = (
            int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) AS m FROM session_turns WHERE session_id=%s",
                    (session_id,),
                ).fetchone()["m"]
            )
            + 1
        )
        turn_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO session_turns(id,session_id,task_id,head_sha,trigger,sequence,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (turn_id, session_id, task_id, head_sha, trigger, sequence, now),
        )
        previous = self._previous_open_snapshot(conn, session_id, turn_id)
        return {
            "session_id": session_id,
            "turn_id": turn_id,
            "sequence": sequence,
            "is_new_session": is_new,
            "previous_head_sha": previous_head,
            "previous_findings": previous,
        }

    @staticmethod
    def _previous_open_snapshot(
        conn, session_id: str, current_turn_id: str
    ) -> list[dict[str, Any]]:
        row = conn.execute(
            "SELECT id FROM session_turns WHERE session_id=%s AND summary_json IS NOT NULL "
            "AND sequence < (SELECT sequence FROM session_turns WHERE id=%s) "
            "ORDER BY sequence DESC LIMIT 1",
            (session_id, current_turn_id),
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT snapshot_json FROM session_findings WHERE turn_id=%s ORDER BY id",
            (row["id"],),
        ).fetchall()
        return [item["snapshot_json"] for item in rows]

    def previous_open_snapshot(self, session_id: str, turn_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return self._previous_open_snapshot(conn, session_id, turn_id)

    def complete_session_turn(
        self,
        session_id: str,
        turn_id: str,
        task_id: str | None,
        open_snapshots: list[dict[str, Any]],
        summary: dict[str, Any],
        head_sha: str | None = None,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("DELETE FROM session_findings WHERE turn_id=%s", (turn_id,))
            for snap in open_snapshots:
                conn.execute(
                    "INSERT INTO session_findings(session_id,turn_id,fingerprint,status,"
                    "snapshot_json,created_at) VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                    (
                        session_id,
                        turn_id,
                        snap.get("fingerprint", ""),
                        snap.get("status", ""),
                        json.dumps(snap, ensure_ascii=False),
                        now,
                    ),
                )
            conn.execute(
                "UPDATE session_turns SET task_id=COALESCE(%s, task_id), summary_json=%s::jsonb, "
                "head_sha=COALESCE(%s, head_sha) WHERE id=%s",
                (task_id, json.dumps(summary, ensure_ascii=False), head_sha, turn_id),
            )
            conn.execute(
                "UPDATE review_sessions SET latest_head_sha=COALESCE(%s, latest_head_sha), "
                "updated_at=%s WHERE id=%s",
                (head_sha, now, session_id),
            )

    def get_session(
        self, tenant_id: str, repository: str, pull_request: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_sessions "
                "WHERE tenant_id=%s AND repository=%s AND pull_request=%s",
                (tenant_id, repository, pull_request),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            value["created_at"] = value["created_at"].isoformat()
            value["updated_at"] = value["updated_at"].isoformat()
            return value

    def get_session_timeline(
        self, session_id: str, tenant_id: str | None = None, turn_limit: int = 200
    ) -> dict[str, Any] | None:
        turn_limit = max(1, min(turn_limit, 500))
        with self._connect() as conn:
            query = "SELECT * FROM review_sessions WHERE id=%s"
            params: list[Any] = [session_id]
            if tenant_id is not None:
                query += " AND tenant_id=%s"
                params.append(tenant_id)
            srow = conn.execute(query, params).fetchone()
            if not srow:
                return None
            turns = conn.execute(
                "SELECT id,task_id,head_sha,trigger,sequence,summary_json,created_at "
                "FROM session_turns WHERE session_id=%s ORDER BY sequence DESC LIMIT %s",
                (session_id, turn_limit),
            ).fetchall()
            turns = list(reversed(turns))
            timeline = dict(srow)
            timeline["created_at"] = timeline["created_at"].isoformat()
            timeline["updated_at"] = timeline["updated_at"].isoformat()
            turn_list = []
            for turn in turns:
                item = dict(turn)
                item["summary"] = item.pop("summary_json")
                item["created_at"] = item["created_at"].isoformat()
                findings = conn.execute(
                    "SELECT snapshot_json FROM session_findings WHERE turn_id=%s ORDER BY id",
                    (item["id"],),
                ).fetchall()
                item["findings"] = [row["snapshot_json"] for row in findings]
                turn_list.append(item)
            timeline["turns"] = turn_list
            return timeline

    def set_session_input_required(self, session_id: str, prompt: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE review_sessions SET status='input-required', pending_input=%s, "
                "updated_at=%s WHERE id=%s",
                (prompt[:4000], utc_now(), session_id),
            )

    def resolve_session_input(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE review_sessions SET status='open', pending_input=NULL, "
                "updated_at=%s WHERE id=%s",
                (utc_now(), session_id),
            )

    def list_tasks(self, limit: int = 50, tenant_id: str | None = None) -> list:
        with self._connect() as conn:
            where = " WHERE tenant_id=%s" if tenant_id is not None else ""
            params = ([tenant_id] if tenant_id is not None else []) + [max(1, min(limit, 200))]
            rows = conn.execute(
                "SELECT id,state,repository,pull_request,error,created_at,updated_at,tenant_id "
                "FROM tasks" + where + " ORDER BY created_at DESC LIMIT %s",
                params,
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["created_at"] = value["created_at"].isoformat()
            value["updated_at"] = value["updated_at"].isoformat()
        return values

    def record_failure_case(self, task_id: str, category: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        if category == "execution_error":
            payload = {
                "error": preserve_safe_summary(payload.get("error"), "review execution failed")
            }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO failure_cases(task_id,category,payload_json,created_at) VALUES (%s,%s,%s::jsonb,%s)",
                (task_id, category, json.dumps(payload, ensure_ascii=False), utc_now()),
            )

    def list_failure_cases(
        self,
        unresolved_only: bool = False,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list:
        joins = " f"
        clauses = []
        params: list[Any] = []
        if tenant_id is not None:
            joins += " JOIN tasks t ON t.id=f.task_id"
            clauses.append("t.tenant_id=%s")
            params.append(tenant_id)
        if unresolved_only:
            clauses.append("f.resolved=FALSE")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT f.* FROM failure_cases" + joins + where + " ORDER BY f.id DESC LIMIT %s",
                params,
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["payload"] = value.pop("payload_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def resolve_failure_cases(self, case_ids: list) -> None:
        ids = [int(value) for value in case_ids]
        if not ids:
            return
        with self._connect() as conn:
            conn.execute("UPDATE failure_cases SET resolved=TRUE WHERE id=ANY(%s)", (ids,))

    def save_evaluation_case(
        self,
        name: str,
        split: str,
        diff: str,
        expected: list,
        source: str = "manual",
        active: bool = True,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO evaluation_cases(name,split,diff,expected_json,source,active,created_at) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s) ON CONFLICT(name) DO NOTHING RETURNING *",
                (
                    name,
                    split,
                    diff,
                    json.dumps(expected, ensure_ascii=False),
                    source,
                    active,
                    utc_now(),
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM evaluation_cases WHERE name=%s", (name,)
                ).fetchone()
                if row["split"] != split or row["diff"] != diff or row["expected_json"] != expected:
                    raise ValueError(
                        "evaluation case names are immutable; use a new name for revised content"
                    )
        value = dict(row)
        value["expected"] = value.pop("expected_json")
        value["created_at"] = value["created_at"].isoformat()
        return value

    def list_evaluation_cases(
        self,
        split: str | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list:
        clauses = []
        params: list[Any] = []
        if split:
            clauses.append("split=%s")
            params.append(split)
        if active_only:
            clauses.append("active=TRUE")
        query = "SELECT * FROM evaluation_cases"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id LIMIT %s"
        params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["expected"] = value.pop("expected_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def save_evolution_run(self, run: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO evolution_runs(id,skill_name,candidate_version,baseline_version,decision,"
                "candidate_score,baseline_score,metrics_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    run["id"],
                    run["skill_name"],
                    run["candidate_version"],
                    run.get("baseline_version"),
                    run["decision"],
                    run["candidate_score"],
                    run["baseline_score"],
                    json.dumps(run["metrics"], ensure_ascii=False),
                    run["created_at"],
                ),
            )
        return run

    def list_evolution_runs(self, limit: int = 50) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM evolution_runs ORDER BY created_at DESC LIMIT %s",
                (max(1, min(limit, 200)),),
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["metrics"] = value.pop("metrics_json")
            value["created_at"] = value["created_at"].isoformat()
        return values

    def get_active_skill_version(self, skill_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s AND active=TRUE ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return dict(row) if row else None

    def save_skill_version(
        self, skill_name: str, prompt: str, score: float, activate: bool = False
    ) -> dict[str, Any]:
        active = self.get_active_skill_version(skill_name)
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (skill_name,))
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_versions WHERE skill_name=%s",
                (skill_name,),
            ).fetchone()
            version = int(row["version"]) + 1
            if activate:
                conn.execute(
                    "UPDATE skill_versions SET active=FALSE WHERE skill_name=%s", (skill_name,)
                )
            conn.execute(
                "INSERT INTO skill_versions(skill_name,version,prompt,score,active,parent_version,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    skill_name,
                    version,
                    prompt,
                    score,
                    activate,
                    active["version"] if active else None,
                    utc_now(),
                ),
            )
        return {"skill_name": skill_name, "version": version, "score": score, "active": activate}

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM skill_versions WHERE skill_name=%s ORDER BY version DESC",
                    (skill_name,),
                ).fetchall()
            )

    def activate_skill_version(self, skill_name: str, version: int) -> bool:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM skill_versions WHERE skill_name=%s AND version=%s",
                (skill_name, version),
            ).fetchone()
            if not exists:
                return False
            conn.execute(
                "UPDATE skill_versions SET active=FALSE WHERE skill_name=%s", (skill_name,)
            )
            conn.execute(
                "UPDATE skill_versions SET active=TRUE WHERE skill_name=%s AND version=%s",
                (skill_name, version),
            )
        return True

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(task_id) DO UPDATE SET diff=EXCLUDED.diff,created_at=EXCLUDED.created_at",
                (task_id, diff, utc_now()),
            )

    def update_task_input(self, task_id: str, updates: dict[str, Any]) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT input_json FROM tasks WHERE id=%s", (task_id,)).fetchone()
            if not row:
                raise ValueError("task not found")
            value = dict(row["input_json"])
            value.update(updates)
            conn.execute(
                "UPDATE tasks SET input_json=%s::jsonb,updated_at=%s WHERE id=%s",
                (json.dumps(value, ensure_ascii=False), utc_now(), task_id),
            )

    def get_task_payload(self, task_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT diff FROM task_payloads WHERE task_id=%s", (task_id,)
            ).fetchone()
        return row["diff"] if row else None

    def save_checkpoint(
        self,
        task_id: str,
        node: str,
        state: dict[str, Any],
        status: str = "completed",
        attempt: int = 1,
        error: str = "",
    ) -> None:
        if error:
            error = preserve_safe_summary(error, "review node failed")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,error,updated_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(task_id,node) DO UPDATE SET "
                "status=EXCLUDED.status,attempt=EXCLUDED.attempt,state_json=EXCLUDED.state_json,"
                "error=EXCLUDED.error,updated_at=EXCLUDED.updated_at",
                (
                    task_id,
                    node,
                    status,
                    attempt,
                    json.dumps(state, ensure_ascii=False),
                    error[:2000] or None,
                    utc_now(),
                ),
            )

    def load_checkpoints(self, task_id: str) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT node,status,attempt,state_json,error,updated_at FROM checkpoints "
                "WHERE task_id=%s ORDER BY updated_at",
                (task_id,),
            ).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["state"] = item.pop("state_json")
            item["updated_at"] = item["updated_at"].isoformat()
            result[item.pop("node")] = item
        return result

    def request_cancel(self, task_id: str, tenant_id: str | None = None) -> bool:
        query = "UPDATE tasks SET cancel_requested=TRUE,updated_at=%s WHERE id=%s"
        params = [utc_now(), task_id]
        if tenant_id is not None:
            query += " AND tenant_id=%s"
            params.append(tenant_id)
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.rowcount > 0

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id=%s", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def cancel(self, task_id: str, event: TraceEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,updated_at=%s WHERE id=%s",
                (TaskState.CANCELLED.value, event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )

    def claim_webhook(
        self,
        delivery_id: str,
        tenant_id: str,
        event_type: str,
        payload_sha256: str,
    ) -> bool:
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO webhook_deliveries"
                "(delivery_id,tenant_id,event_type,payload_sha256,received_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(delivery_id) DO NOTHING RETURNING delivery_id",
                (delivery_id, tenant_id, event_type, payload_sha256, utc_now()),
            ).fetchone()
            if row:
                return True
            existing = conn.execute(
                "SELECT payload_sha256 FROM webhook_deliveries WHERE delivery_id=%s",
                (delivery_id,),
            ).fetchone()
            if existing and existing["payload_sha256"] != payload_sha256:
                raise ValueError("delivery id was already used with a different payload")
            return False

    def accept_pull_request_webhook(
        self,
        delivery_id: str,
        tenant_id: str,
        payload_sha256: str,
        repository: str,
        pull_request: int,
        head_sha: str | None,
        trigger: str,
        task_id: str,
        task_payload: dict[str, Any],
        outbox_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically bind one delivery to its session, task, and queue intent."""
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        now = utc_now()
        with self._connect() as conn:
            inserted = conn.execute(
                "INSERT INTO webhook_deliveries"
                "(delivery_id,tenant_id,event_type,payload_sha256,received_at) "
                "VALUES (%s,%s,'pull_request',%s,%s) "
                "ON CONFLICT(delivery_id) DO NOTHING RETURNING delivery_id",
                (delivery_id, tenant_id, payload_sha256, now),
            ).fetchone()
            if not inserted:
                existing = conn.execute(
                    "SELECT payload_sha256,task_id FROM webhook_deliveries WHERE delivery_id=%s",
                    (delivery_id,),
                ).fetchone()
                if not existing:
                    raise RuntimeError("webhook delivery conflict could not be resolved")
                if existing and existing["payload_sha256"] != payload_sha256:
                    raise ValueError("delivery id was already used with a different payload")
                if existing and existing["task_id"]:
                    return {"accepted": False, "task_id": existing["task_id"]}
            session = self._start_session_turn_in_transaction(
                conn,
                tenant_id,
                repository,
                pull_request,
                head_sha,
                trigger,
                task_id,
                now,
            )
            session_payload = {
                "session_id": session["session_id"],
                "turn_id": session["turn_id"],
                "head_sha": head_sha,
            }
            conn.execute(
                "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,error,"
                "created_at,updated_at,tenant_id,cancel_requested) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,NULL,NULL,%s,%s,%s,FALSE)",
                (
                    task_id,
                    TaskState.PENDING.value,
                    repository,
                    pull_request,
                    json.dumps({**task_payload, **session_payload}, ensure_ascii=False),
                    now,
                    now,
                    tenant_id,
                ),
            )
            conn.execute(
                "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                "attempts,available_at,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s::jsonb,'pending',0,%s,%s,%s)",
                (
                    "review:" + task_id,
                    "review",
                    task_id,
                    json.dumps(
                        {"task_id": task_id, **outbox_payload, **session_payload},
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=%s WHERE delivery_id=%s",
                (task_id, delivery_id),
            )
        return {"accepted": True, "task_id": task_id, **session}

    def complete_webhook(self, delivery_id: str, task_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE webhook_deliveries SET task_id=%s WHERE delivery_id=%s",
                (task_id, delivery_id),
            )

    def get_webhook(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webhook_deliveries WHERE delivery_id=%s", (delivery_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        tenant_id: str,
        role: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(username) DO NOTHING",
                (user_id, username, password_hash, utc_now()),
            )
            row = conn.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
            conn.execute(
                "INSERT INTO memberships(user_id,tenant_id,role) VALUES (%s,%s,%s) "
                "ON CONFLICT(user_id,tenant_id) DO UPDATE SET role=EXCLUDED.role",
                (row["id"], tenant_id, role),
            )

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,username,password_hash,active FROM users WHERE username=%s",
                (username,),
            ).fetchone()
            if not row:
                return None
            memberships = conn.execute(
                "SELECT tenant_id,role FROM memberships WHERE user_id=%s", (row["id"],)
            ).fetchall()
        value = dict(row)
        value["memberships"] = [dict(item) for item in memberships]
        return value

    def grant_repository(self, tenant_id: str, repository: str, auto_fix: bool = False) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO repository_grants(tenant_id,repository,auto_fix) VALUES (%s,%s,%s) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET auto_fix=EXCLUDED.auto_fix",
                (tenant_id, repository, auto_fix),
            )

    def save_repository_policy(
        self,
        tenant_id: str,
        repository: str,
        policy: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        serialized = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("repository-policy:%s:%s" % (tenant_id, repository),),
            )
            row = conn.execute(
                "SELECT version FROM repository_policies "
                "WHERE tenant_id=%s AND repository=%s FOR UPDATE",
                (tenant_id, repository),
            ).fetchone()
            version = int(row["version"]) + 1 if row else 1
            conn.execute(
                "INSERT INTO repository_policies(tenant_id,repository,version,enabled,auto_fix,"
                "policy_json,updated_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET version=EXCLUDED.version,"
                "enabled=EXCLUDED.enabled,auto_fix=EXCLUDED.auto_fix,"
                "policy_json=EXCLUDED.policy_json,updated_at=EXCLUDED.updated_at",
                (
                    tenant_id,
                    repository,
                    version,
                    bool(policy["enabled"]),
                    bool(policy["auto_fix"]),
                    serialized,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO repository_policy_versions(tenant_id,repository,version,"
                "policy_json,actor,created_at) VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                (tenant_id, repository, version, serialized, actor, now),
            )
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    tenant_id,
                    actor,
                    "repository-policy.updated",
                    repository,
                    json.dumps({"version": version, "policy": policy}, ensure_ascii=False),
                    now,
                ),
            )
        return {
            "tenant_id": tenant_id,
            "repository": repository,
            "version": version,
            "policy": dict(policy),
            "actor": actor,
            "updated_at": now,
        }

    def get_repository_policy(self, tenant_id: str, repository: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id,repository,version,policy_json,updated_at "
                "FROM repository_policies WHERE tenant_id=%s AND repository=%s",
                (tenant_id, repository),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        raw_policy = value.pop("policy_json")
        value["policy"] = json.loads(raw_policy) if isinstance(raw_policy, str) else raw_policy
        return value

    def list_repository_policy_versions(
        self, tenant_id: str, repository: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tenant_id,repository,version,policy_json,actor,created_at "
                "FROM repository_policy_versions WHERE tenant_id=%s AND repository=%s "
                "ORDER BY version DESC LIMIT %s",
                (tenant_id, repository, max(1, min(limit, 200))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            raw_policy = item.pop("policy_json")
            item["policy"] = json.loads(raw_policy) if isinstance(raw_policy, str) else raw_policy
            values.append(item)
        return values

    def repository_allowed(
        self,
        tenant_id: str,
        repository: str,
        require_auto_fix: bool = False,
    ) -> bool:
        with self._connect() as conn:
            policy = conn.execute(
                "SELECT enabled,auto_fix FROM repository_policies "
                "WHERE tenant_id=%s AND repository=%s",
                (tenant_id, repository),
            ).fetchone()
            if policy:
                return bool(policy["enabled"] and (not require_auto_fix or policy["auto_fix"]))
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=%s", (tenant_id,)
            ).fetchone()["n"]
            row = conn.execute(
                "SELECT auto_fix FROM repository_grants WHERE tenant_id=%s AND repository=%s",
                (tenant_id, repository),
            ).fetchone()
        return True if total == 0 else bool(row and (not require_auto_fix or row["auto_fix"]))

    def audit(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        resource: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if action == "shadow.failed":
            detail = {
                "error": preserve_safe_summary((detail or {}).get("error"), "shadow review failed")
            }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    tenant_id,
                    actor,
                    action,
                    resource,
                    json.dumps(detail or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def list_audit(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT actor,action,resource,detail_json,created_at FROM audit_log "
                "WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [
            {**dict(row), "detail": row["detail_json"], "created_at": row["created_at"].isoformat()}
            for row in rows
        ]

    def save_deployment(self, tenant_id: str, skill_name: str, config: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments(tenant_id,skill_name,stable_version,candidate_version,"
                "canary_percent,shadow_percent,max_error_rate,min_samples,status,samples,errors,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s) "
                "ON CONFLICT(tenant_id,skill_name) DO UPDATE SET stable_version=EXCLUDED.stable_version,"
                "candidate_version=EXCLUDED.candidate_version,canary_percent=EXCLUDED.canary_percent,"
                "shadow_percent=EXCLUDED.shadow_percent,max_error_rate=EXCLUDED.max_error_rate,"
                "min_samples=EXCLUDED.min_samples,status=EXCLUDED.status,samples=0,errors=0,"
                "updated_at=EXCLUDED.updated_at",
                (
                    tenant_id,
                    skill_name,
                    config.get("stable_version"),
                    config.get("candidate_version"),
                    int(config.get("canary_percent", 0)),
                    int(config.get("shadow_percent", 0)),
                    float(config.get("max_error_rate", 0.1)),
                    int(config.get("min_samples", 20)),
                    config.get("status", "running"),
                    utc_now(),
                ),
            )
            conn.execute(
                "UPDATE deployments SET max_disagreement_rate=%s,auto_promote=%s,"
                "shadow_samples=0,disagreements=0 WHERE tenant_id=%s AND skill_name=%s",
                (
                    float(config.get("max_disagreement_rate", 0.2)),
                    bool(config.get("auto_promote", False)),
                    tenant_id,
                    skill_name,
                ),
            )

    def get_deployment(self, tenant_id: str, skill_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE tenant_id=%s AND skill_name=%s",
                (tenant_id, skill_name),
            ).fetchone()
        return dict(row) if row else None

    def record_deployment_result(
        self,
        tenant_id: str,
        skill_name: str,
        failed: bool,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE deployments SET samples=samples+1,errors=errors+%s,updated_at=%s "
                "WHERE tenant_id=%s AND skill_name=%s RETURNING *",
                (int(failed), utc_now(), tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            if (
                value["status"] == "running"
                and value["samples"] >= value["min_samples"]
                and value["errors"] / value["samples"] > value["max_error_rate"]
            ):
                conn.execute(
                    "UPDATE deployments SET status='rolled_back',canary_percent=0,shadow_percent=0,"
                    "updated_at=%s WHERE tenant_id=%s AND skill_name=%s",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "rolled_back"
        return value

    def record_shadow_observation(
        self,
        tenant_id: str,
        skill_name: str,
        task_id: str,
        lane: str,
        primary: dict[str, Any],
        candidate: dict[str, Any] | None,
        disagreement: float,
        candidate_failed: bool = False,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO release_observations(tenant_id,skill_name,task_id,lane,"
                "primary_json,candidate_json,disagreement,candidate_failed,created_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                (
                    tenant_id,
                    skill_name,
                    task_id,
                    lane,
                    json.dumps(primary, ensure_ascii=False),
                    json.dumps(candidate, ensure_ascii=False) if candidate is not None else None,
                    float(disagreement),
                    candidate_failed,
                    utc_now(),
                ),
            )
            row = conn.execute(
                "UPDATE deployments SET shadow_samples=shadow_samples+1,"
                "disagreements=disagreements+%s,updated_at=%s "
                "WHERE tenant_id=%s AND skill_name=%s RETURNING *",
                (int(disagreement > 0), utc_now(), tenant_id, skill_name),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            disagreement_rate = (
                value["disagreements"] / value["shadow_samples"] if value["shadow_samples"] else 0.0
            )
            error_rate = value["errors"] / value["samples"] if value["samples"] else 0.0
            if (
                value["status"] == "running"
                and value["auto_promote"]
                and value["shadow_samples"] >= value["min_samples"]
                and disagreement_rate <= value["max_disagreement_rate"]
                and error_rate <= value["max_error_rate"]
                and not candidate_failed
            ):
                conn.execute(
                    "UPDATE deployments SET status='promoted',stable_version=candidate_version,"
                    "canary_percent=0,shadow_percent=0,updated_at=%s "
                    "WHERE tenant_id=%s AND skill_name=%s",
                    (utc_now(), tenant_id, skill_name),
                )
                value["status"] = "promoted"
        return value

    def list_release_observations(
        self,
        tenant_id: str,
        skill_name: str,
        limit: int = 100,
    ) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM release_observations WHERE tenant_id=%s AND skill_name=%s "
                "ORDER BY id DESC LIMIT %s",
                (tenant_id, skill_name, max(1, min(limit, 500))),
            ).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            item["primary"] = item.pop("primary_json")
            item["candidate"] = item.pop("candidate_json")
            values.append(item)
        return values

    def create_alert(
        self,
        tenant_id: str,
        alert_key: str,
        severity: str,
        message: str,
    ) -> None:
        if alert_key.startswith("dlq:"):
            message = preserve_safe_summary(message, "task delivery failed")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alerts(tenant_id,alert_key,severity,message,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'open',%s,%s) ON CONFLICT(tenant_id,alert_key,status) DO NOTHING",
                (tenant_id, alert_key, severity, message[:1000], utc_now(), utc_now()),
            )

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_installation(
        self, installation_id: int, account_login: str, tenant_id: str = "default"
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO installations(installation_id,account_login,created_at,tenant_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(installation_id) DO UPDATE "
                "SET account_login=EXCLUDED.account_login,created_at=EXCLUDED.created_at,"
                "tenant_id=EXCLUDED.tenant_id",
                (installation_id, account_login, utc_now(), tenant_id),
            )

    def installation_tenant(self, installation_id: int) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id FROM installations WHERE installation_id=%s",
                (installation_id,),
            ).fetchone()
        return row["tenant_id"] if row else None

    def dashboard_stats(self, tenant_id: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            where = " WHERE tenant_id=%s" if tenant_id is not None else ""
            params = (tenant_id,) if tenant_id is not None else ()
            row = conn.execute(
                "SELECT COUNT(*) AS total,COUNT(*) FILTER(WHERE state='SUCCESS') AS success,"
                "COUNT(*) FILTER(WHERE state='FAILED') AS failed FROM tasks" + where,
                params,
            ).fetchone()
            if tenant_id is None:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases WHERE resolved=FALSE"
                ).fetchone()["n"]
            else:
                failures = conn.execute(
                    "SELECT COUNT(*) AS n FROM failure_cases f JOIN tasks t ON t.id=f.task_id "
                    "WHERE f.resolved=FALSE AND t.tenant_id=%s",
                    (tenant_id,),
                ).fetchone()["n"]
            skills = conn.execute(
                "SELECT COUNT(*) AS n FROM skill_versions WHERE active=TRUE"
            ).fetchone()["n"]
        return {
            "tasks_total": row["total"],
            "tasks_success": row["success"],
            "tasks_failed": row["failed"],
            "success_rate": round(row["success"] / row["total"], 4) if row["total"] else 0.0,
            "unresolved_failure_cases": failures,
            "active_skill_versions": skills,
        }


def create_store(
    database_url: str,
    sqlite_path: str,
    pool_min: int = 1,
    pool_max: int = 10,
    pool_timeout: float = 10.0,
) -> ApplicationStorePort:
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresTaskStore(database_url, pool_min, pool_max, pool_timeout)
    from .store import TaskStore

    return TaskStore(sqlite_path)
