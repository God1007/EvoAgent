"""PostgreSQL persistence backend."""

import json
import math
import uuid
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from .errors import (
    AccessDeniedError,
    ClientInputError,
    TenantReviewCapacityError,
    preserve_safe_summary,
)
from .migrations import migrate_postgres, validate_current_schema_history
from .models import ReviewReport, TaskState, TraceEvent
from .repository import canonical_repository
from .time_utils import utc_after, utc_now

_TASK_PROGRESS = {
    TaskState.PENDING.value: 0,
    TaskState.PLANNING.value: 1,
    TaskState.EXECUTING.value: 2,
    TaskState.REVIEWING.value: 3,
}
MAX_PG_POOL_SIZE = 256


def _valid_admission_generation(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


class PostgresTaskStore:
    def __init__(
        self,
        url: str,
        pool_min: int = 1,
        pool_max: int = 10,
        pool_timeout: float = 10.0,
        statement_timeout_seconds: float = 120.0,
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
        if not math.isfinite(self.pool_timeout) or self.pool_timeout <= 0:
            raise ValueError("EVOAGENT_PG_POOL_TIMEOUT must be positive")
        self._connect_timeout = max(1, math.ceil(self.pool_timeout))
        if not math.isfinite(statement_timeout_seconds) or statement_timeout_seconds <= 0:
            raise ValueError("EVOAGENT_PG_STATEMENT_TIMEOUT_SECONDS must be positive")
        self.statement_timeout_seconds = float(statement_timeout_seconds)
        self._connection_options = "-c statement_timeout=%d" % max(
            1, math.ceil(self.statement_timeout_seconds * 1000)
        )
        self.auto_migrate = bool(auto_migrate)
        # A real connection pool avoids a TCP connect + auth handshake on every
        # single query (the previous per-call `psycopg.connect` was the dominant
        # Postgres cost under load). Keep the import guard so a deliberately
        # unpooled maintenance caller can still opt out with pool_max=0.
        self._pool = None
        try:
            if pool_max and pool_max > 0:
                try:
                    from psycopg_pool import ConnectionPool
                except ImportError as exc:
                    raise RuntimeError(
                        "PostgreSQL pooling requires: pip install psycopg-pool"
                    ) from exc
                # open=False + explicit open() avoids the deprecated eager
                # constructor-open path in psycopg_pool >= 3.2.
                self._pool = ConnectionPool(
                    conninfo=url,
                    min_size=pool_min,
                    max_size=pool_max,
                    timeout=self.pool_timeout,
                    kwargs={
                        "row_factory": dict_row,
                        "connect_timeout": self._connect_timeout,
                        "options": self._connection_options,
                    },
                    open=False,
                )
                self._pool.open()
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
        return self.psycopg.connect(
            self.url,
            row_factory=self.dict_row,
            connect_timeout=self._connect_timeout,
            options=self._connection_options,
        )

    def has_pool(self) -> bool:
        return self._pool is not None

    @staticmethod
    def _admission_generation_matches(conn, task_id: str, generation: int | None) -> bool:
        if generation is None:
            return True
        if not _valid_admission_generation(generation):
            return False
        row = conn.execute(
            "SELECT generation FROM task_admissions WHERE task_id=%s AND active=TRUE",
            (task_id,),
        ).fetchone()
        return bool(row and int(row["generation"]) == generation)

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
                migrate_postgres(conn)
            else:
                rows = conn.execute(
                    "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                validate_current_schema_history(list(rows))

    def schema_version(self) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        return validate_current_schema_history(list(rows))

    def prune_operational_history(
        self,
        trace_before: str,
        session_before: str,
        batch_size: int,
        pruned_at: str,
    ) -> dict[str, int]:
        """Prune inactive history in bounded, replica-safe transactions."""
        bounded = max(1, min(int(batch_size), 10_000))
        terminal_states = (
            TaskState.SUCCESS.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        )
        with self._connect() as conn:
            trace_rows = conn.execute(
                "SELECT events.id,events.task_id FROM trace_events AS events "
                "JOIN tasks AS task ON task.id=events.task_id "
                "WHERE events.created_at<%s AND task.state=ANY(%s) "
                "AND events.id<>(SELECT MAX(latest.id) FROM trace_events AS latest "
                "WHERE latest.task_id=events.task_id) "
                "ORDER BY events.created_at,events.id LIMIT %s "
                "FOR UPDATE OF events SKIP LOCKED",
                (trace_before, list(terminal_states), bounded),
            ).fetchall()
            trace_ids = [int(row["id"]) for row in trace_rows]
            task_ids = sorted({str(row["task_id"]) for row in trace_rows})
            if trace_ids:
                conn.execute("DELETE FROM trace_events WHERE id=ANY(%s)", (trace_ids,))
                conn.execute(
                    "UPDATE tasks SET trace_pruned_at=COALESCE(trace_pruned_at,%s) "
                    "WHERE id=ANY(%s)",
                    (pruned_at, task_ids),
                )

            artifact_rows = conn.execute(
                "SELECT task.id FROM tasks AS task WHERE task.updated_at<%s "
                "AND task.state=ANY(%s) "
                "AND NOT (task.input_json ? 'execution_artifacts_pruned_at') "
                "AND NOT EXISTS (SELECT 1 FROM task_admissions AS admission "
                "WHERE admission.task_id=task.id AND admission.active=TRUE) "
                "ORDER BY task.updated_at,task.id LIMIT %s FOR UPDATE OF task SKIP LOCKED",
                (
                    trace_before,
                    [TaskState.SUCCESS.value, TaskState.CANCELLED.value],
                    bounded,
                ),
            ).fetchall()
            artifact_task_ids = [str(row["id"]) for row in artifact_rows]
            payloads_pruned = 0
            checkpoints_pruned = 0
            messages_pruned = 0
            if artifact_task_ids:
                payloads_pruned = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM task_payloads WHERE task_id=ANY(%s)",
                        (artifact_task_ids,),
                    ).fetchone()["count"]
                )
                checkpoints_pruned = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM checkpoints WHERE task_id=ANY(%s)",
                        (artifact_task_ids,),
                    ).fetchone()["count"]
                )
                messages_pruned = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM agent_messages WHERE task_id=ANY(%s)",
                        (artifact_task_ids,),
                    ).fetchone()["count"]
                )
                conn.execute(
                    "DELETE FROM task_payloads WHERE task_id=ANY(%s)", (artifact_task_ids,)
                )
                conn.execute("DELETE FROM checkpoints WHERE task_id=ANY(%s)", (artifact_task_ids,))
                conn.execute(
                    "DELETE FROM agent_messages WHERE task_id=ANY(%s)", (artifact_task_ids,)
                )
                conn.execute(
                    "UPDATE tasks SET input_json=input_json||"
                    "jsonb_build_object('execution_artifacts_pruned_at',%s::text) "
                    "WHERE id=ANY(%s)",
                    (pruned_at, artifact_task_ids),
                )

            outbox_tasks = conn.execute(
                "SELECT task.id FROM tasks AS task WHERE task.updated_at<%s "
                "AND (task.state=%s OR (task.state=%s AND "
                "task.input_json @> '{\"_delivery_complete\":true}'::jsonb)) "
                "AND NOT EXISTS (SELECT 1 FROM task_admissions AS admission "
                "WHERE admission.task_id=task.id AND admission.active=TRUE) "
                "AND EXISTS (SELECT 1 FROM outbox_messages AS outbox "
                "WHERE outbox.id='review:'||task.id) "
                "ORDER BY task.updated_at,task.id LIMIT %s FOR UPDATE OF task SKIP LOCKED",
                (
                    trace_before,
                    TaskState.CANCELLED.value,
                    TaskState.SUCCESS.value,
                    bounded,
                ),
            ).fetchall()
            # ponytail: primary intents dominate growth; index payload task_id only if
            # manual resume/recovery Outbox history becomes material.
            outbox_ids = ["review:" + str(row["id"]) for row in outbox_tasks]
            if outbox_ids:
                conn.execute("DELETE FROM outbox_messages WHERE id=ANY(%s)", (outbox_ids,))

            effect_rows = conn.execute(
                "WITH candidates AS (SELECT receipt.effect_key FROM effect_receipts AS receipt "
                "WHERE receipt.status='completed' AND receipt.completed_at<%s "
                "ORDER BY receipt.completed_at,receipt.effect_key LIMIT %s "
                "FOR UPDATE OF receipt SKIP LOCKED) "
                "DELETE FROM effect_receipts AS receipt USING candidates "
                "WHERE receipt.effect_key=candidates.effect_key RETURNING receipt.effect_key",
                (trace_before, bounded),
            ).fetchall()

            webhook_rows = conn.execute(
                "WITH candidates AS (SELECT delivery.delivery_id FROM webhook_deliveries AS delivery "
                "WHERE delivery.received_at<%s ORDER BY delivery.received_at,delivery.delivery_id "
                "LIMIT %s FOR UPDATE OF delivery SKIP LOCKED) "
                "DELETE FROM webhook_deliveries AS delivery USING candidates "
                "WHERE delivery.delivery_id=candidates.delivery_id RETURNING delivery.delivery_id",
                (trace_before, bounded),
            ).fetchall()

            release_rows = conn.execute(
                "WITH candidates AS (SELECT observation.id FROM release_observations AS observation "
                "WHERE observation.created_at<%s AND NOT EXISTS (SELECT 1 FROM deployments "
                "AS deployment WHERE deployment.tenant_id=observation.tenant_id "
                "AND deployment.skill_name=observation.skill_name AND deployment.status='running') "
                "ORDER BY observation.created_at,observation.id LIMIT %s "
                "FOR UPDATE OF observation SKIP LOCKED) "
                "DELETE FROM release_observations AS observation USING candidates "
                "WHERE observation.id=candidates.id RETURNING observation.id",
                (trace_before, bounded),
            ).fetchall()

            candidates = conn.execute(
                "SELECT turn.id,turn.session_id FROM session_turns AS turn "
                "WHERE turn.summary_json IS NOT NULL AND turn.findings_pruned_at IS NULL "
                "AND EXISTS (SELECT 1 FROM session_findings AS finding "
                "WHERE finding.turn_id=turn.id AND finding.created_at<%s) "
                "AND NOT EXISTS (SELECT 1 FROM session_findings AS finding "
                "WHERE finding.turn_id=turn.id AND finding.created_at>=%s) "
                "AND EXISTS (SELECT 1 FROM session_turns AS later "
                "WHERE later.session_id=turn.session_id AND later.summary_json IS NOT NULL "
                "AND later.sequence>turn.sequence) "
                "AND NOT EXISTS (SELECT 1 FROM session_turns AS pending "
                "WHERE pending.session_id=turn.session_id AND pending.summary_json IS NULL "
                "AND pending.sequence>turn.sequence AND NOT EXISTS ("
                "SELECT 1 FROM session_turns AS middle "
                "WHERE middle.session_id=turn.session_id AND middle.summary_json IS NOT NULL "
                "AND middle.sequence>turn.sequence AND middle.sequence<pending.sequence)) "
                "ORDER BY turn.created_at,turn.id LIMIT %s",
                (session_before, session_before, bounded),
            ).fetchall()
            candidate_ids = [str(row["id"]) for row in candidates]
            for session_id in sorted({str(row["session_id"]) for row in candidates}):
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("session-state:%s" % session_id,),
                )

            turn_ids: list[str] = []
            if candidate_ids:
                # Re-evaluate after acquiring every session lock: a new pending
                # turn may have appeared between candidate discovery and lock.
                eligible = conn.execute(
                    "SELECT turn.id FROM session_turns AS turn "
                    "WHERE turn.id=ANY(%s) AND turn.summary_json IS NOT NULL "
                    "AND turn.findings_pruned_at IS NULL "
                    "AND EXISTS (SELECT 1 FROM session_findings AS finding "
                    "WHERE finding.turn_id=turn.id AND finding.created_at<%s) "
                    "AND NOT EXISTS (SELECT 1 FROM session_findings AS finding "
                    "WHERE finding.turn_id=turn.id AND finding.created_at>=%s) "
                    "AND EXISTS (SELECT 1 FROM session_turns AS later "
                    "WHERE later.session_id=turn.session_id AND later.summary_json IS NOT NULL "
                    "AND later.sequence>turn.sequence) "
                    "AND NOT EXISTS (SELECT 1 FROM session_turns AS pending "
                    "WHERE pending.session_id=turn.session_id AND pending.summary_json IS NULL "
                    "AND pending.sequence>turn.sequence AND NOT EXISTS ("
                    "SELECT 1 FROM session_turns AS middle "
                    "WHERE middle.session_id=turn.session_id AND middle.summary_json IS NOT NULL "
                    "AND middle.sequence>turn.sequence AND middle.sequence<pending.sequence))",
                    (candidate_ids, session_before, session_before),
                ).fetchall()
                turn_ids = [str(row["id"]) for row in eligible]
            findings_pruned = 0
            if turn_ids:
                findings_pruned = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM session_findings WHERE turn_id=ANY(%s)",
                        (turn_ids,),
                    ).fetchone()["count"]
                )
                conn.execute(
                    "UPDATE session_turns SET findings_pruned_at=%s WHERE id=ANY(%s)",
                    (pruned_at, turn_ids),
                )
                conn.execute("DELETE FROM session_findings WHERE turn_id=ANY(%s)", (turn_ids,))
        return {
            "trace_events": len(trace_ids),
            "execution_tasks": len(artifact_task_ids),
            "task_payloads": payloads_pruned,
            "checkpoints": checkpoints_pruned,
            "agent_messages": messages_pruned,
            "outbox_messages": len(outbox_ids),
            "effect_receipts": len(effect_rows),
            "webhook_deliveries": len(webhook_rows),
            "release_observations": len(release_rows),
            "session_turns": len(turn_ids),
            "session_findings": findings_pruned,
        }

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
        max_active_reviews: int = 0,
        actor: str = "",
    ) -> bool:
        """Persist task, optional diff, queue intent, and idempotent replay in one transaction."""
        now = utc_now()
        limit = max(0, int(max_active_reviews))
        rejected = False
        with self._connect() as conn:
            self._lock_tenant_admission(conn, tenant_id)
            fingerprint = payload.get("idempotency_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                existing = conn.execute(
                    "SELECT tenant_id,input_json FROM tasks WHERE id=%s FOR UPDATE",
                    (task_id,),
                ).fetchone()
                if existing:
                    existing_input = existing["input_json"]
                    if (
                        existing["tenant_id"] != tenant_id
                        or not isinstance(existing_input, dict)
                        or existing_input.get("idempotency_fingerprint") != fingerprint
                    ):
                        raise ClientInputError(
                            "Idempotency-Key was already used with a different review"
                        )
                    if actor:
                        self._audit_review_create(
                            conn, tenant_id, actor, repository, outbox_payload is not None, now
                        )
                    return False
            active = self._active_admission_count(conn, tenant_id)
            if limit and active >= limit:
                self._record_admission_rejection(
                    conn,
                    tenant_id,
                    repository,
                    active,
                    limit,
                    str(payload.get("source", "review")),
                    now,
                )
                rejected = True
            else:
                conn.execute(
                    "INSERT INTO tasks(id,state,repository,pull_request,input_json,report_json,"
                    "error,created_at,updated_at,tenant_id,cancel_requested) "
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
                conn.execute(
                    "INSERT INTO task_admissions(task_id,tenant_id,active,release_on_failure,"
                    "generation,acquired_at) VALUES (%s,%s,TRUE,%s,1,%s)",
                    (task_id, tenant_id, outbox_payload is None, now),
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
                            json.dumps(
                                {**outbox_payload, "admission_generation": 1},
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                            now,
                        ),
                    )
                if actor:
                    self._audit_review_create(
                        conn, tenant_id, actor, repository, outbox_payload is not None, now
                    )
        if rejected:
            raise TenantReviewCapacityError()
        return True

    @staticmethod
    def _audit_review_create(
        conn: Any,
        tenant_id: str,
        actor: str,
        repository: str,
        asynchronous: bool,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
            "VALUES (%s,%s,'review.create',%s,%s::jsonb,%s)",
            (tenant_id, actor, repository, json.dumps({"async": asynchronous}), now),
        )

    @staticmethod
    def _lock_tenant_admission(conn, tenant_id: str) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("tenant-review-admission:%s" % tenant_id,),
        )

    @staticmethod
    def _active_admission_count(conn, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM task_admissions WHERE active=TRUE"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM task_admissions WHERE tenant_id=%s AND active=TRUE",
                (tenant_id,),
            ).fetchone()
        return int(row["count"])

    @staticmethod
    def _record_admission_rejection(
        conn,
        tenant_id: str,
        resource: str,
        active: int,
        limit: int,
        source: str,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
            "VALUES (%s,'system','review.capacity-rejected',%s,%s::jsonb,%s)",
            (
                tenant_id,
                resource[:250],
                json.dumps(
                    {"active": active, "limit": limit, "source": source[:64]},
                    ensure_ascii=False,
                ),
                now,
            ),
        )

    def tenant_review_admission_stats(self, tenant_id: str | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            if tenant_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS active,MIN(acquired_at) AS oldest_acquired_at "
                    "FROM task_admissions WHERE active=TRUE"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS active,MIN(acquired_at) AS oldest_acquired_at "
                    "FROM task_admissions WHERE tenant_id=%s AND active=TRUE",
                    (tenant_id,),
                ).fetchone()
        oldest = row["oldest_acquired_at"]
        return {
            "active": int(row["active"]),
            "oldest_acquired_at": oldest.isoformat() if oldest is not None else None,
        }

    def release_review_admission(
        self, task_id: str, reason: str, generation: int | None = None
    ) -> bool:
        if reason not in {"success", "cancelled", "failed", "dead-letter"}:
            raise ValueError("unsupported review admission release reason")
        if generation is not None and not _valid_admission_generation(generation):
            return False
        with self._connect() as conn:
            query = (
                "UPDATE task_admissions SET active=FALSE,released_at=%s,release_reason=%s "
                "WHERE task_id=%s AND active=TRUE"
            )
            params: list[Any] = [utc_now(), reason, task_id]
            if generation is not None:
                query += " AND generation=%s"
                params.append(generation)
            cursor = conn.execute(query, params)
        return cursor.rowcount > 0

    def review_admission_active(self, task_id: str, generation: object = None) -> bool:
        if generation is not None and not _valid_admission_generation(generation):
            return False
        generation_filter = " AND generation=%s" if generation is not None else ""
        params = (task_id, generation) if generation is not None else (task_id,)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM task_admissions WHERE task_id=%s "
                "AND active=TRUE" + generation_filter + ") AS active",
                params,
            ).fetchone()
        return bool(row["active"])

    def resume_review_task(
        self,
        task_id: str,
        tenant_id: str,
        max_active_reviews: int,
        outbox_id: str,
        message_key: str,
        outbox_payload: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, Any]:
        now = utc_now()
        limit = max(0, int(max_active_reviews))
        rejected = False
        result: dict[str, Any]
        with self._connect() as conn:
            self._lock_tenant_admission(conn, tenant_id)
            row = conn.execute(
                "SELECT task.state,task.repository,admission.active,admission.generation "
                "FROM tasks AS task "
                "LEFT JOIN task_admissions AS admission ON admission.task_id=task.id "
                "WHERE task.id=%s AND task.tenant_id=%s FOR UPDATE OF task",
                (task_id, tenant_id),
            ).fetchone()
            if row is None:
                result = {"status": "missing"}
            elif row["state"] in {TaskState.SUCCESS.value, TaskState.CANCELLED.value}:
                result = {"status": str(row["state"]).lower()}
            elif bool(row["active"]):
                result = {"status": "active"}
            else:
                active = self._active_admission_count(conn, tenant_id)
                if limit and active >= limit:
                    self._record_admission_rejection(
                        conn,
                        tenant_id,
                        str(row["repository"]),
                        active,
                        limit,
                        "resume",
                        now,
                    )
                    rejected = True
                    result = {"status": "rejected"}
                else:
                    generation = int(row["generation"] or 0) + 1
                    conn.execute(
                        "INSERT INTO task_admissions(task_id,tenant_id,active,"
                        "release_on_failure,generation,acquired_at,released_at,release_reason) "
                        "VALUES (%s,%s,TRUE,FALSE,%s,%s,NULL,NULL) "
                        "ON CONFLICT(task_id) DO UPDATE SET "
                        "tenant_id=EXCLUDED.tenant_id,active=TRUE,release_on_failure=FALSE,"
                        "generation=EXCLUDED.generation,acquired_at=EXCLUDED.acquired_at,"
                        "released_at=NULL,release_reason=NULL",
                        (task_id, tenant_id, generation, now),
                    )
                    conn.execute(
                        "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                        "attempts,available_at,created_at,updated_at) "
                        "VALUES (%s,'review',%s,%s::jsonb,'pending',0,%s,%s,%s)",
                        (
                            outbox_id,
                            message_key,
                            json.dumps(
                                {**outbox_payload, "admission_generation": generation},
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        "UPDATE tasks SET state=%s,error=NULL,updated_at=%s WHERE id=%s",
                        (TaskState.PENDING.value, now, task_id),
                    )
                    result = {"status": "resumed", "generation": generation}
            self._audit_task_resume(conn, tenant_id, actor, task_id, result["status"], now)
        if rejected:
            raise TenantReviewCapacityError()
        return result

    def resume_review_delivery(
        self,
        task_id: str,
        tenant_id: str,
        outbox_id: str,
        message_key: str,
        outbox_payload: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, str]:
        now = utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state,input_json FROM tasks WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (task_id, tenant_id),
            ).fetchone()
            if row is None:
                result = {"status": "missing"}
            elif row["state"] != TaskState.SUCCESS.value:
                result = {"status": str(row["state"]).lower()}
            else:
                task_input = row["input_json"]
                if task_input.get("_delivery_complete") is True:
                    result = {"status": "complete"}
                else:
                    result = {}
                    if task_input.get("_delivery_resume_active") is True:
                        current_outbox_id = task_input.get("_delivery_resume_outbox_id")
                        current_outbox = (
                            conn.execute(
                                "SELECT status FROM outbox_messages WHERE id=%s",
                                (current_outbox_id,),
                            ).fetchone()
                            if isinstance(current_outbox_id, str) and current_outbox_id
                            else None
                        )
                        if current_outbox and current_outbox["status"] != "dead":
                            result = {"status": "active"}
                    if not result:
                        task_input.update(
                            {
                                "_delivery_resume_active": True,
                                "_delivery_resume_outbox_id": outbox_id,
                            }
                        )
                        conn.execute(
                            "UPDATE tasks SET input_json=%s::jsonb,updated_at=%s WHERE id=%s",
                            (json.dumps(task_input, ensure_ascii=False), now, task_id),
                        )
                        conn.execute(
                            "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                            "attempts,available_at,created_at,updated_at) "
                            "VALUES (%s,'review',%s,%s::jsonb,'pending',0,%s,%s,%s)",
                            (
                                outbox_id,
                                message_key,
                                json.dumps(outbox_payload, ensure_ascii=False),
                                now,
                                now,
                                now,
                            ),
                        )
                        result = {"status": "resumed"}
            self._audit_task_resume(conn, tenant_id, actor, task_id, result["status"], now)
        return result

    @staticmethod
    def _audit_task_resume(
        conn: Any,
        tenant_id: str,
        actor: str,
        task_id: str,
        status: str,
        now: str,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
            "VALUES (%s,%s,'task.resume',%s,%s::jsonb,%s)",
            (tenant_id, actor, task_id, json.dumps({"status": status}), now),
        )

    def release_review_delivery_resume(self, task_id: str, outbox_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET input_json=input_json || %s::jsonb,updated_at=%s "
                "WHERE id=%s AND input_json->>'_delivery_resume_outbox_id'=%s",
                (
                    json.dumps({"_delivery_resume_active": False}),
                    utc_now(),
                    task_id,
                    outbox_id,
                ),
            )
        return cursor.rowcount > 0

    def claim_outbox(
        self,
        owner: str,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> list[dict[str, Any]]:
        now = utc_now()
        lease_until = utc_after(lease_seconds)
        expired_error = preserve_safe_summary(None, "outbox dispatch failed")
        batch_limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                "WITH expired_candidates AS (SELECT id FROM outbox_messages "
                "WHERE status='publishing' AND lease_until<%s AND attempts>=%s "
                "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED),"
                "expired AS (UPDATE outbox_messages AS message SET status='dead',"
                "lease_owner=NULL,lease_until=NULL,last_error=%s,updated_at=%s "
                "FROM expired_candidates WHERE message.id=expired_candidates.id RETURNING message.id),"
                "candidates AS (SELECT id FROM outbox_messages "
                "WHERE attempts < %s AND "
                "((status='pending' AND available_at<=%s) OR "
                "(status='publishing' AND lease_until<%s)) "
                "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED) "
                "UPDATE outbox_messages AS message SET status='publishing',"
                "attempts=message.attempts+1,lease_owner=%s,lease_until=%s,updated_at=%s "
                "FROM candidates WHERE message.id=candidates.id RETURNING message.*",
                (
                    now,
                    max_attempts,
                    batch_limit,
                    expired_error,
                    now,
                    max_attempts,
                    now,
                    now,
                    batch_limit,
                    owner,
                    lease_until,
                    now,
                ),
            ).fetchall()
            claimed = []
            for row in rows:
                item = dict(row)
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

    def list_outbox(
        self,
        status: str = "dead",
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list:
        if status not in {"pending", "publishing", "published", "dead"}:
            raise ValueError("unsupported outbox status")
        bounded = max(1, min(limit, 500))
        query = "SELECT * FROM outbox_messages WHERE status=%s ORDER BY created_at DESC LIMIT %s"
        params: tuple[Any, ...] = (status, bounded)
        if tenant_id is not None:
            query = (
                "SELECT outbox.* FROM outbox_messages AS outbox JOIN tasks AS task "
                "ON task.id=outbox.payload_json->>'task_id' WHERE outbox.status=%s "
                "AND task.tenant_id=%s ORDER BY outbox.created_at DESC LIMIT %s"
            )
            params = (status, tenant_id, bounded)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        values = []
        for row in rows:
            item = dict(row)
            raw_payload = item.pop("payload_json")
            item["payload"] = (
                json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            )
            values.append(item)
        return values

    def requeue_outbox(self, message_id: str, tenant_id: str, actor: str) -> bool:
        now = utc_now()
        with self._connect() as conn:
            replayed = (
                conn.execute(
                    "UPDATE outbox_messages AS outbox SET status='pending',attempts=0,"
                    "available_at=%s,lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=%s "
                    "FROM tasks AS task WHERE outbox.id=%s AND outbox.status='dead' "
                    "AND task.id=outbox.payload_json->>'task_id' AND task.tenant_id=%s "
                    "RETURNING outbox.id",
                    (now, now, message_id, tenant_id),
                ).fetchone()
                is not None
            )
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'outbox.replay',%s,%s::jsonb,%s)",
                (tenant_id, actor, message_id, json.dumps({"replayed": replayed}), now),
            )
        return replayed

    def queue_recovery_candidates(self, limit: int) -> list[dict[str, Any]]:
        """Describe incomplete or delivery-managed task intents without mutation."""
        bounded = max(1, min(int(limit), 100_001))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.id,t.repository,t.pull_request,t.tenant_id,t.state,t.input_json,"
                "o.status AS outbox_status,o.id AS outbox_id,o.payload_json,"
                "(p.task_id IS NOT NULL) AS has_payload,"
                "a.generation AS admission_generation "
                "FROM tasks t LEFT JOIN outbox_messages o ON o.id=CASE "
                "WHEN t.state=%s AND t.input_json @> "
                "'{\"_delivery_resume_active\":true}'::jsonb "
                "THEN t.input_json->>'_delivery_resume_outbox_id' ELSE 'review:'||t.id END "
                "LEFT JOIN task_payloads p ON p.task_id=t.id "
                "LEFT JOIN task_admissions a ON a.task_id=t.id "
                "WHERE t.cancel_requested=FALSE AND (t.state NOT IN (%s,%s,%s) "
                "OR (t.state=%s AND a.active=TRUE) OR (t.state=%s AND t.input_json @> "
                "'{\"_delivery_resume_active\":true}'::jsonb AND NOT t.input_json @> "
                "'{\"_delivery_complete\":true}'::jsonb)) "
                "ORDER BY t.created_at,t.id LIMIT %s",
                (
                    TaskState.SUCCESS.value,
                    TaskState.SUCCESS.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                    TaskState.FAILED.value,
                    TaskState.SUCCESS.value,
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
                if row["has_payload"] and row["state"] != TaskState.SUCCESS.value
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
                if row["admission_generation"] is not None:
                    payload["admission_generation"] = int(row["admission_generation"])
            recoverable = isinstance(payload, dict)
            candidates.append(
                {
                    "task_id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "outbox_id": row["outbox_id"]
                    or (
                        str(row["input_json"].get("_delivery_resume_outbox_id") or "")
                        if row["state"] == TaskState.SUCCESS.value
                        else "review:" + str(row["id"])
                    ),
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
                    "SELECT state,cancel_requested,tenant_id,input_json,EXISTS(SELECT 1 FROM "
                    "task_admissions a WHERE a.task_id=tasks.id AND a.active=TRUE) "
                    "AS admission_active FROM tasks WHERE id=%s FOR UPDATE",
                    (candidate["task_id"],),
                ).fetchone()
                payload = candidate.get("payload")
                task_input = task["input_json"] if task else {}
                delivery_resume = bool(
                    task
                    and task["state"] == TaskState.SUCCESS.value
                    and isinstance(payload, dict)
                    and payload.get("delivery_only") is True
                    and task_input.get("_delivery_resume_active") is True
                    and task_input.get("_delivery_complete") is not True
                    and candidate.get("outbox_id") == task_input.get("_delivery_resume_outbox_id")
                )
                if (
                    not task
                    or task["state"] == TaskState.CANCELLED.value
                    or (task["state"] == TaskState.SUCCESS.value and not delivery_resume)
                    or (
                        task["state"] == TaskState.FAILED.value
                        and not bool(task["admission_active"])
                    )
                    or bool(task["cancel_requested"])
                ):
                    skipped_terminal += 1
                    continue
                if not isinstance(payload, dict):
                    skipped_unrecoverable += 1
                    continue
                message_id = str(candidate.get("outbox_id") or "")
                expected_message_id = (
                    str(task_input.get("_delivery_resume_outbox_id") or "")
                    if delivery_resume
                    else "review:" + candidate["task_id"]
                )
                if message_id != expected_message_id:
                    skipped_unrecoverable += 1
                    continue
                serialized = json.dumps(payload, ensure_ascii=False)
                outbox = conn.execute(
                    "SELECT status FROM outbox_messages WHERE id=%s FOR UPDATE", (message_id,)
                ).fetchone()
                if outbox and outbox["status"] in {"published", "dead"}:
                    recovery_message_id = "recovery:%s:%s" % (
                        recovery_id,
                        candidate["task_id"],
                    )
                    recovery_message_key = (
                        recovery_message_id
                        if delivery_resume
                        else "%s:%s" % (recovery_id, candidate["task_id"])
                    )
                    conn.execute(
                        "INSERT INTO outbox_messages(id,topic,message_key,payload_json,status,"
                        "attempts,available_at,created_at,updated_at) "
                        "VALUES (%s,'review',%s,%s::jsonb,'pending',0,%s,%s,%s)",
                        (
                            recovery_message_id,
                            recovery_message_key,
                            serialized,
                            now,
                            now,
                            now,
                        ),
                    )
                    if delivery_resume:
                        conn.execute(
                            "UPDATE tasks SET input_json=input_json||"
                            "jsonb_build_object('_delivery_resume_outbox_id',%s::text),updated_at=%s "
                            "WHERE id=%s",
                            (recovery_message_key, now, candidate["task_id"]),
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

    def complete_effect(
        self,
        effect_key: str,
        owner: str,
        result: dict[str, Any],
        audit_event: tuple[str, str, str, str, dict[str, Any]] | None = None,
    ) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE effect_receipts SET status='completed',result_json=%s::jsonb,owner=NULL,"
                "lease_until=NULL,last_error=NULL,updated_at=%s,completed_at=%s "
                "WHERE effect_key=%s AND status='in-progress' AND owner=%s",
                (json.dumps(result, ensure_ascii=False), now, now, effect_key, owner),
            )
            if cursor.rowcount > 0 and audit_event is not None:
                tenant_id, actor, action, resource, detail = audit_event
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                    (
                        tenant_id,
                        actor,
                        action,
                        resource,
                        json.dumps(detail, ensure_ascii=False),
                        now,
                    ),
                )
        return cursor.rowcount > 0

    def renew_effect(self, effect_key: str, owner: str, lease_seconds: float) -> bool:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE effect_receipts SET lease_until=%s,updated_at=%s "
                "WHERE effect_key=%s AND status='in-progress' AND owner=%s",
                (utc_after(lease_seconds), now, effect_key, owner),
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

    def transition(self, task_id: str, event: TraceEvent, generation: int | None = None) -> bool:
        with self._connect() as conn:
            task = conn.execute(
                "SELECT state,cancel_requested FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if not task or bool(task["cancel_requested"]):
                return False
            if not self._admission_generation_matches(conn, task_id, generation):
                return False
            if task["state"] == TaskState.SUCCESS.value:
                return True
            current_progress = _TASK_PROGRESS.get(task["state"])
            target_progress = _TASK_PROGRESS.get(event.state.value)
            if (
                current_progress is not None
                and target_progress is not None
                and current_progress >= target_progress
            ):
                return True
            conn.execute(
                "UPDATE tasks SET state=%s,updated_at=%s WHERE id=%s",
                (event.state.value, event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
            )
        return True

    def succeed(
        self,
        task_id: str,
        report: ReviewReport,
        event: TraceEvent,
        generation: int | None = None,
    ) -> bool:
        with self._connect() as conn:
            task = conn.execute(
                "SELECT state,cancel_requested FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if not task or bool(task["cancel_requested"]):
                return False
            if not self._admission_generation_matches(conn, task_id, generation):
                return False
            if task["state"] == TaskState.SUCCESS.value:
                return True
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
            conn.execute(
                "UPDATE failure_cases SET resolved=TRUE WHERE task_id=%s "
                "AND category='execution_error' AND resolved=FALSE",
                (task_id,),
            )
            conn.execute(
                "UPDATE task_admissions SET active=FALSE,released_at=%s,"
                "release_reason='success' WHERE task_id=%s AND active=TRUE",
                (event.created_at, task_id),
            )
        return True

    def fail(
        self,
        task_id: str,
        error: str,
        event: TraceEvent,
        generation: int | None = None,
    ) -> bool:
        error = preserve_safe_summary(error, "review execution failed")
        with self._connect() as conn:
            task = conn.execute(
                "SELECT state,cancel_requested FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if not task or bool(task["cancel_requested"]):
                return False
            if not self._admission_generation_matches(conn, task_id, generation):
                return False
            if task["state"] == TaskState.SUCCESS.value:
                return True
            conn.execute(
                "UPDATE tasks SET state=%s,error=%s,updated_at=%s WHERE id=%s",
                (TaskState.FAILED.value, error[:2000], event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, error, event.created_at),
            )
            conn.execute(
                "UPDATE task_admissions SET active=FALSE,released_at=%s,"
                "release_reason='failed' WHERE task_id=%s AND active=TRUE "
                "AND release_on_failure=TRUE",
                (event.created_at, task_id),
            )
        return True

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
        if value.get("trace_pruned_at") is not None:
            value["trace_pruned_at"] = value["trace_pruned_at"].isoformat()
        for item in value["trace"]:
            item["created_at"] = item["created_at"].isoformat()
        return value

    def record_agent_message(
        self, task_id: str, message: dict[str, Any], generation: int | None = None
    ) -> bool:
        content = dict(message.get("content", {}))
        if message.get("kind") == "agent_failure":
            content = {"error": preserve_safe_summary(content.get("error"), "review agent failed")}
        with self._connect() as conn:
            task = conn.execute(
                "SELECT state,cancel_requested FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if (
                not task
                or bool(task["cancel_requested"])
                or task["state"] == TaskState.SUCCESS.value
                or not self._admission_generation_matches(conn, task_id, generation)
            ):
                return False
            inserted = conn.execute(
                "INSERT INTO agent_messages(task_id,sender,recipient,kind,correlation_id,"
                "content_json,created_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING id",
                (
                    task_id,
                    message["sender"],
                    message["recipient"],
                    message["kind"],
                    message.get("correlation_id", ""),
                    json.dumps(content, ensure_ascii=False),
                    utc_now(),
                ),
            ).fetchone()
        return inserted is not None

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
        event_at: datetime | None = None,
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
            "latest_head_sha,last_webhook_at,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,'open',%s,%s,%s,%s) "
            "ON CONFLICT(tenant_id,repository,pull_request) DO NOTHING RETURNING id",
            (new_id, tenant_id, repository, pull_request, head_sha, event_at, now, now),
        ).fetchone()
        is_new = inserted is not None
        row = conn.execute(
            "SELECT id, latest_head_sha FROM review_sessions "
            "WHERE tenant_id=%s AND repository=%s AND pull_request=%s",
            (tenant_id, repository, pull_request),
        ).fetchone()
        session_id = row["id"]
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("session-state:%s" % session_id,),
        )
        if not is_new:
            conn.execute(
                "UPDATE review_sessions SET status='open',pending_input=NULL,"
                "latest_head_sha=COALESCE(%s,latest_head_sha),"
                "last_webhook_at=COALESCE(%s,last_webhook_at),updated_at=%s WHERE id=%s",
                (head_sha, event_at, now, session_id),
            )
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
    ) -> bool:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("session-state:%s" % session_id,),
            )
            turn = conn.execute(
                "SELECT summary_json,findings_pruned_at FROM session_turns "
                "WHERE id=%s AND session_id=%s",
                (turn_id, session_id),
            ).fetchone()
            if not turn:
                raise ValueError("session turn not found")
            if turn["summary_json"] is not None and turn.get("findings_pruned_at") is None:
                return False
            conn.execute("DELETE FROM session_findings WHERE turn_id=%s", (turn_id,))
            if open_snapshots:
                conn.execute(
                    "INSERT INTO session_findings(session_id,turn_id,fingerprint,status,"
                    "snapshot_json,created_at) SELECT %s,%s,"
                    "COALESCE(snapshot->>'fingerprint',''),"
                    "COALESCE(snapshot->>'status',''),snapshot,%s "
                    "FROM jsonb_array_elements(%s::jsonb) WITH ORDINALITY "
                    "AS item(snapshot,position) ORDER BY position",
                    (
                        session_id,
                        turn_id,
                        now,
                        json.dumps(open_snapshots, ensure_ascii=False),
                    ),
                )
            conn.execute(
                "UPDATE session_turns SET task_id=COALESCE(%s, task_id), summary_json=%s::jsonb, "
                "head_sha=COALESCE(%s, head_sha),findings_pruned_at=NULL WHERE id=%s",
                (task_id, json.dumps(summary, ensure_ascii=False), head_sha, turn_id),
            )
            conn.execute(
                "UPDATE review_sessions AS session SET latest_head_sha=CASE WHEN NOT EXISTS ("
                "SELECT 1 FROM session_turns AS later WHERE later.session_id=session.id "
                "AND later.sequence>(SELECT current.sequence FROM session_turns AS current "
                "WHERE current.id=%s)) THEN COALESCE(%s,session.latest_head_sha) "
                "ELSE session.latest_head_sha END,updated_at=%s WHERE session.id=%s",
                (turn_id, head_sha, now, session_id),
            )
        return True

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
            if value["last_webhook_at"] is not None:
                value["last_webhook_at"] = value["last_webhook_at"].isoformat()
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
                "SELECT id,task_id,head_sha,trigger,sequence,summary_json,created_at,"
                "findings_pruned_at "
                "FROM session_turns WHERE session_id=%s ORDER BY sequence DESC LIMIT %s",
                (session_id, turn_limit),
            ).fetchall()
            turns = list(reversed(turns))
            findings_by_turn: dict[str, list[Any]] = {turn["id"]: [] for turn in turns}
            if findings_by_turn:
                findings = conn.execute(
                    "SELECT turn_id,snapshot_json FROM session_findings "
                    "WHERE turn_id=ANY(%s) ORDER BY id",
                    (list(findings_by_turn),),
                ).fetchall()
                for finding in findings:
                    findings_by_turn[finding["turn_id"]].append(finding["snapshot_json"])
            timeline = dict(srow)
            timeline["created_at"] = timeline["created_at"].isoformat()
            timeline["updated_at"] = timeline["updated_at"].isoformat()
            if timeline["last_webhook_at"] is not None:
                timeline["last_webhook_at"] = timeline["last_webhook_at"].isoformat()
            turn_list = []
            for turn in turns:
                item = dict(turn)
                item["summary"] = item.pop("summary_json")
                item["created_at"] = item["created_at"].isoformat()
                if item["findings_pruned_at"] is not None:
                    item["findings_pruned_at"] = item["findings_pruned_at"].isoformat()
                item["findings_retained"] = item["findings_pruned_at"] is None
                item["findings"] = findings_by_turn[item["id"]]
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

    def resolve_session_input(
        self,
        session_id: str,
        tenant_id: str | None = None,
        actor: str = "system",
    ) -> bool:
        tenant_filter = " AND tenant_id=%s" if tenant_id is not None else ""
        now = utc_now()
        params = (now, session_id, tenant_id) if tenant_id is not None else (now, session_id)
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE review_sessions SET status='open', pending_input=NULL, "
                "updated_at=%s WHERE id=%s AND status='input-required'"
                + tenant_filter
                + " RETURNING tenant_id",
                params,
            ).fetchone()
            if not updated:
                return False
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'session.input.provided',%s,'{}'::jsonb,%s)",
                (updated["tenant_id"], actor, session_id, now),
            )
        return True

    def list_tasks(self, limit: int = 50, tenant_id: str | None = None) -> list:
        with self._connect() as conn:
            where = " WHERE tenant_id=%s" if tenant_id is not None else ""
            params = ([tenant_id] if tenant_id is not None else []) + [max(1, min(limit, 200))]
            rows = conn.execute(
                "SELECT id,state,repository,pull_request,error,created_at,updated_at,tenant_id,"
                "(state='FAILED' AND EXISTS (SELECT 1 FROM task_admissions AS admission WHERE "
                "admission.task_id=tasks.id AND admission.active=TRUE)) AS retrying "
                "FROM tasks" + where + " ORDER BY created_at DESC LIMIT %s",
                params,
            ).fetchall()
        values = [dict(row) for row in rows]
        for value in values:
            value["created_at"] = value["created_at"].isoformat()
            value["updated_at"] = value["updated_at"].isoformat()
        return values

    def tenant_task_ids(self, tenant_id: str, task_ids: list[str]) -> set[str]:
        ids = list(dict.fromkeys(task_id for task_id in task_ids if task_id))[:500]
        if not ids:
            return set()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE tenant_id=%s AND id=ANY(%s)",
                (tenant_id, ids),
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def record_failure_case(
        self,
        task_id: str,
        category: str,
        payload: dict[str, Any],
        generation: int | None = None,
        tenant_id: str | None = None,
        actor: str = "",
    ) -> bool:
        if generation is not None and not _valid_admission_generation(generation):
            return False
        payload = dict(payload)
        if category == "execution_error":
            payload = {
                "error": preserve_safe_summary(payload.get("error"), "review execution failed")
            }
        now = utc_now()
        with self._connect() as conn:
            query = (
                "SELECT task.state,task.tenant_id,admission.generation FROM tasks AS task "
                "LEFT JOIN task_admissions AS admission ON admission.task_id=task.id "
                "WHERE task.id=%s"
            )
            params: list[Any] = [task_id]
            if tenant_id is not None:
                query += " AND task.tenant_id=%s"
                params.append(tenant_id)
            task = conn.execute(query + " FOR UPDATE OF task", params).fetchone()
            if not task:
                return False
            if generation is not None and task["generation"] != generation:
                return False
            if category == "execution_error" and task["state"] == TaskState.SUCCESS.value:
                return False
            conn.execute(
                "INSERT INTO failure_cases(task_id,category,payload_json,created_at) VALUES (%s,%s,%s::jsonb,%s)",
                (task_id, category, json.dumps(payload, ensure_ascii=False), now),
            )
            if actor:
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,%s,'review.feedback',%s,%s::jsonb,%s)",
                    (
                        task["tenant_id"],
                        actor,
                        task_id,
                        json.dumps({"category": category}),
                        now,
                    ),
                )
        return True

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

    def get_skill_evaluation_revision(self, skill_name: str, version: int) -> str:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("skill version must be a positive integer")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metrics_json #>> '{reproducibility,execution_revision}' AS revision "
                "FROM evolution_runs WHERE skill_name=%s AND candidate_version=%s "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (skill_name, version),
            ).fetchone()
        return str(row["revision"] or "") if row else ""

    def get_active_skill_version(self, skill_name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s AND active=TRUE ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
        return dict(row) if row else None

    def get_skill_version(self, skill_name: str, version: int) -> dict[str, Any] | None:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("skill version must be a positive integer")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s AND version=%s",
                (skill_name, version),
            ).fetchone()
        return dict(row) if row else None

    def get_skill_version_by_prompt(self, skill_name: str, prompt: str) -> dict[str, Any] | None:
        if not isinstance(prompt, str):
            raise ValueError("skill prompt must be a string")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_versions WHERE skill_name=%s AND BTRIM(prompt)=%s "
                "ORDER BY version DESC LIMIT 1",
                (skill_name, prompt),
            ).fetchone()
        return dict(row) if row else None

    def save_skill_version(
        self,
        skill_name: str,
        prompt: str,
        score: float,
        qualification: str = "rejected",
    ) -> dict[str, Any]:
        if (
            not isinstance(skill_name, str)
            or not skill_name
            or skill_name != skill_name.strip()
            or len(skill_name) > 120
        ):
            raise ValueError("skill name must be 1-120 characters without surrounding whitespace")
        if not isinstance(prompt, str) or len(prompt) > 12_000:
            raise ValueError("skill prompt must be a string of at most 12000 characters")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
        ):
            raise ValueError("skill score must be a finite number between 0 and 1")
        if not isinstance(qualification, str) or qualification not in {
            "legacy",
            "approved",
            "rejected",
            "deferred",
        }:
            raise ValueError("skill qualification is invalid")
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (skill_name,))
            active = conn.execute(
                "SELECT version FROM skill_versions WHERE skill_name=%s AND active=TRUE "
                "ORDER BY version DESC LIMIT 1",
                (skill_name,),
            ).fetchone()
            row = conn.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM skill_versions WHERE skill_name=%s",
                (skill_name,),
            ).fetchone()
            version = int(row["version"]) + 1
            conn.execute(
                "INSERT INTO skill_versions(skill_name,version,prompt,score,active,qualification,"
                "parent_version,created_at) VALUES (%s,%s,%s,%s,FALSE,%s,%s,%s)",
                (
                    skill_name,
                    version,
                    prompt,
                    score,
                    qualification,
                    active["version"] if active else None,
                    utc_now(),
                ),
            )
        return {
            "skill_name": skill_name,
            "version": version,
            "score": score,
            "active": False,
            "qualification": qualification,
        }

    def list_skill_versions(self, skill_name: str) -> list:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM skill_versions WHERE skill_name=%s ORDER BY version DESC",
                    (skill_name,),
                ).fetchall()
            )

    def save_task_payload(self, task_id: str, diff: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO task_payloads(task_id,diff,created_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(task_id) DO UPDATE SET diff=EXCLUDED.diff,created_at=EXCLUDED.created_at",
                (task_id, diff, utc_now()),
            )

    def update_task_input(self, task_id: str, updates: dict[str, Any]) -> None:
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE tasks SET input_json=input_json || %s::jsonb,updated_at=%s "
                "WHERE id=%s RETURNING id",
                (json.dumps(updates, ensure_ascii=False), utc_now(), task_id),
            ).fetchone()
            if not updated:
                raise ValueError("task not found")

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
        generation: int | None = None,
    ) -> bool:
        if error:
            error = preserve_safe_summary(error, "review node failed")
        with self._connect() as conn:
            task = conn.execute(
                "SELECT state,cancel_requested FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
            ).fetchone()
            if not task:
                raise ValueError("task not found")
            if bool(task["cancel_requested"]):
                return False
            if not self._admission_generation_matches(conn, task_id, generation):
                return False
            if task["state"] == TaskState.SUCCESS.value:
                return True
            conn.execute(
                "INSERT INTO checkpoints(task_id,node,status,attempt,state_json,error,updated_at) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(task_id,node) DO UPDATE SET "
                "status=EXCLUDED.status,attempt=EXCLUDED.attempt,state_json=EXCLUDED.state_json,"
                "error=EXCLUDED.error,updated_at=EXCLUDED.updated_at "
                "WHERE checkpoints.status<>'completed' AND "
                "(EXCLUDED.status='completed' OR EXCLUDED.attempt>=checkpoints.attempt)",
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
        return True

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

    def request_cancel(self, task_id: str, tenant_id: str, actor: str = "system") -> bool:
        now = utc_now()
        query = (
            "SELECT state,EXISTS(SELECT 1 FROM task_admissions AS admission "
            "WHERE admission.task_id=tasks.id AND admission.active=TRUE) AS admission_active "
            "FROM tasks WHERE id=%s AND tenant_id=%s FOR UPDATE"
        )
        with self._connect() as conn:
            task = conn.execute(query, (task_id, tenant_id)).fetchone()
            state = str(task["state"]) if task else ""
            changed = False
            if task:
                if state == TaskState.PENDING.value or (
                    state == TaskState.FAILED.value and bool(task["admission_active"])
                ):
                    row = conn.execute(
                        "SELECT COALESCE(MAX(step),0) AS step FROM trace_events WHERE task_id=%s",
                        (task_id,),
                    ).fetchone()
                    changed = self._cancel_in_transaction(
                        conn,
                        task_id,
                        TraceEvent(
                            int(row["step"]) + 1,
                            TaskState.CANCELLED,
                            "Task was cancelled",
                            now,
                        ),
                    )
                elif state not in {
                    TaskState.SUCCESS.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                }:
                    changed = (
                        conn.execute(
                            "UPDATE tasks SET cancel_requested=TRUE,updated_at=%s WHERE id=%s",
                            (now, task_id),
                        ).rowcount
                        > 0
                    )
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'task.cancel',%s,%s::jsonb,%s)",
                (
                    tenant_id,
                    actor,
                    task_id,
                    json.dumps(
                        {"accepted": task is not None, "changed": changed, "state": state or None}
                    ),
                    now,
                ),
            )
        return task is not None

    def is_cancelled(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id=%s", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    @staticmethod
    def _cancel_in_transaction(conn, task_id: str, event: TraceEvent) -> bool:
        updated = conn.execute(
            "UPDATE tasks SET state=%s,cancel_requested=TRUE,updated_at=%s WHERE id=%s "
            "AND state NOT IN (%s,%s) RETURNING id",
            (
                TaskState.CANCELLED.value,
                event.created_at,
                task_id,
                TaskState.SUCCESS.value,
                TaskState.CANCELLED.value,
            ),
        ).fetchone()
        if not updated:
            return False
        conn.execute(
            "INSERT INTO trace_events(task_id,step,state,message,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (task_id, event.step, event.state.value, event.message, event.created_at),
        )
        conn.execute(
            "UPDATE task_admissions SET active=FALSE,released_at=%s,"
            "release_reason='cancelled' WHERE task_id=%s AND active=TRUE",
            (event.created_at, task_id),
        )
        return True

    def cancel(self, task_id: str, event: TraceEvent, generation: int | None = None) -> bool:
        with self._connect() as conn:
            if generation is not None:
                task = conn.execute(
                    "SELECT id FROM tasks WHERE id=%s FOR UPDATE", (task_id,)
                ).fetchone()
                if not task or not self._admission_generation_matches(conn, task_id, generation):
                    return False
            return self._cancel_in_transaction(conn, task_id, event)

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

    def finish_pull_request_webhook(
        self,
        delivery_id: str,
        tenant_id: str,
        payload_sha256: str,
        repository: str,
        pull_request: int,
        status: str,
        event_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically end a PR session and cancel work that can no longer publish."""
        if not delivery_id:
            raise ValueError("X-GitHub-Delivery is required")
        if status not in {"closed", "draft"}:
            raise ValueError("invalid pull request session status")
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
                    "SELECT payload_sha256 FROM webhook_deliveries WHERE delivery_id=%s",
                    (delivery_id,),
                ).fetchone()
                if existing and existing["payload_sha256"] != payload_sha256:
                    raise ValueError("delivery id was already used with a different payload")
                return {
                    "accepted": False,
                    "cancelled": 0,
                    "cancel_requested": 0,
                    "released": 0,
                }

            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("session:%s:%s:%s" % (tenant_id, repository, pull_request),),
            )
            new_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO review_sessions(id,tenant_id,repository,pull_request,status,"
                "last_webhook_at,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(tenant_id,repository,pull_request) DO NOTHING",
                (new_id, tenant_id, repository, pull_request, status, event_at, now, now),
            )
            session = conn.execute(
                "SELECT id,last_webhook_at FROM review_sessions "
                "WHERE tenant_id=%s AND repository=%s AND pull_request=%s",
                (tenant_id, repository, pull_request),
            ).fetchone()
            if (
                event_at is not None
                and session["last_webhook_at"] is not None
                and (session["last_webhook_at"] > event_at)
            ):
                return {
                    "accepted": True,
                    "stale": True,
                    "cancelled": 0,
                    "cancel_requested": 0,
                    "released": 0,
                }

            session_id = session["id"]
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("session-state:%s" % session_id,),
            )
            conn.execute(
                "UPDATE review_sessions SET status=%s,pending_input=NULL,"
                "last_webhook_at=COALESCE(%s,last_webhook_at),updated_at=%s WHERE id=%s",
                (status, event_at, now, session_id),
            )
            tasks = conn.execute(
                "SELECT task.id,task.state,EXISTS(SELECT 1 FROM task_admissions AS admission "
                "WHERE admission.task_id=task.id AND admission.active=TRUE) AS admission_active "
                "FROM session_turns AS turn_item JOIN tasks AS task ON task.id=turn_item.task_id "
                "WHERE turn_item.session_id=%s FOR UPDATE OF task",
                (session_id,),
            ).fetchall()
            cancelled = 0
            cancel_requested = 0
            released = 0
            for task in tasks:
                task_id = str(task["id"])
                conn.execute(
                    "UPDATE tasks SET input_json=input_json || %s::jsonb,updated_at=%s WHERE id=%s",
                    (
                        json.dumps({"_delivery_complete": True, "_delivery_resume_active": False}),
                        now,
                        task_id,
                    ),
                )
                state = str(task["state"])
                if state in {TaskState.PENDING.value, TaskState.FAILED.value}:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(step),0) AS step FROM trace_events WHERE task_id=%s",
                        (task_id,),
                    ).fetchone()
                    did_cancel = self._cancel_in_transaction(
                        conn,
                        task_id,
                        TraceEvent(
                            int(row["step"]) + 1,
                            TaskState.CANCELLED,
                            "Pull request review was cancelled",
                            now,
                        ),
                    )
                    cancelled += int(did_cancel)
                    released += int(did_cancel and bool(task["admission_active"]))
                elif state not in {
                    TaskState.SUCCESS.value,
                    TaskState.FAILED.value,
                    TaskState.CANCELLED.value,
                }:
                    conn.execute(
                        "UPDATE tasks SET cancel_requested=TRUE,updated_at=%s WHERE id=%s",
                        (now, task_id),
                    )
                    cancel_requested += 1
        return {
            "accepted": True,
            "session_id": session_id,
            "cancelled": cancelled,
            "cancel_requested": cancel_requested,
            "released": released,
        }

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
        max_active_reviews: int = 0,
        event_at: datetime | None = None,
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
                    "SELECT payload_sha256,task_id FROM webhook_deliveries "
                    "WHERE delivery_id=%s FOR UPDATE",
                    (delivery_id,),
                ).fetchone()
                if not existing:
                    raise RuntimeError("webhook delivery conflict could not be resolved")
                if existing and existing["payload_sha256"] != payload_sha256:
                    raise ValueError("delivery id was already used with a different payload")
                if existing and existing["task_id"]:
                    return {"accepted": False, "task_id": existing["task_id"]}
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("session:%s:%s:%s" % (tenant_id, repository, pull_request),),
            )
            session = conn.execute(
                "SELECT status,last_webhook_at FROM review_sessions "
                "WHERE tenant_id=%s AND repository=%s AND pull_request=%s",
                (tenant_id, repository, pull_request),
            ).fetchone()
            if session and event_at is not None and session["last_webhook_at"] is not None:
                stale = session["last_webhook_at"] > event_at or (
                    session["last_webhook_at"] == event_at
                    and session["status"] in {"closed", "draft"}
                    and trigger not in {"reopened", "ready_for_review"}
                )
                if stale:
                    return {"accepted": True, "stale": True}
            self._lock_tenant_admission(conn, tenant_id)
            limit = max(0, int(max_active_reviews))
            active = self._active_admission_count(conn, tenant_id)
            if limit and active >= limit:
                self._record_admission_rejection(
                    conn,
                    tenant_id,
                    repository,
                    active,
                    limit,
                    "github-webhook",
                    now,
                )
                conn.commit()
                raise TenantReviewCapacityError()
            session = self._start_session_turn_in_transaction(
                conn,
                tenant_id,
                repository,
                pull_request,
                head_sha,
                trigger,
                task_id,
                now,
                event_at,
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
                "INSERT INTO task_admissions(task_id,tenant_id,active,release_on_failure,"
                "generation,acquired_at) VALUES (%s,%s,TRUE,FALSE,1,%s)",
                (task_id, tenant_id, now),
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
                        {
                            "task_id": task_id,
                            **outbox_payload,
                            **session_payload,
                            "admission_generation": 1,
                        },
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
        actor: str = "",
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "INSERT INTO users(id,username,password_hash,created_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT(username) DO NOTHING RETURNING id",
                (user_id, username, password_hash, utc_now()),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "INSERT INTO memberships(user_id,tenant_id,role) VALUES (%s,%s,%s) "
                "ON CONFLICT(user_id,tenant_id) DO NOTHING",
                (row["id"], tenant_id, role),
            )
            if actor:
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,%s,'auth.user-create',%s,%s::jsonb,%s)",
                    (
                        tenant_id,
                        actor,
                        row["id"],
                        json.dumps({"username": username, "role": role}, ensure_ascii=False),
                        utc_now(),
                    ),
                )
        return True

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,username,password_hash,active,credential_version "
                "FROM users WHERE username=%s",
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

    def change_user_password(
        self,
        user_id: str,
        expected_password_hash: str,
        password_hash: str,
        actor: str,
        audit_tenant_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE users SET password_hash=%s,credential_version=credential_version+1 "
                "WHERE id=%s AND password_hash=%s AND active=TRUE RETURNING id",
                (password_hash, user_id, expected_password_hash),
            ).fetchone()
            if not row:
                return False
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'auth.password-change',%s,'{}'::jsonb,%s)",
                (audit_tenant_id, actor, user_id, utc_now()),
            )
        return True

    def set_user_active(
        self,
        user_id: str,
        active: bool,
        actor: str,
        audit_tenant_id: str,
    ) -> bool:
        with self._connect() as conn:
            user = conn.execute(
                "SELECT id,username,active FROM users WHERE id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if not user:
                return False
            if bool(user["active"]) == active:
                return True
            if not active:
                platform_admin = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM memberships "
                    "WHERE user_id=%s AND role='platform_admin') AS present",
                    (user_id,),
                ).fetchone()["present"]
                if platform_admin:
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ("evoagent:platform-admin-status",),
                    )
                    remaining = conn.execute(
                        "SELECT COUNT(DISTINCT users.id) AS count FROM users "
                        "JOIN memberships ON memberships.user_id=users.id "
                        "WHERE users.active=TRUE AND memberships.role='platform_admin'"
                    ).fetchone()["count"]
                    if int(remaining) <= 1:
                        raise ClientInputError(
                            "the last active platform administrator cannot be disabled"
                        )
            conn.execute(
                "UPDATE users SET active=%s,credential_version=credential_version+1 WHERE id=%s",
                (active, user_id),
            )
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'auth.user-status',%s,%s::jsonb,%s)",
                (
                    audit_tenant_id,
                    actor,
                    user_id,
                    json.dumps(
                        {"username": user["username"], "active": active}, ensure_ascii=False
                    ),
                    utc_now(),
                ),
            )
        return True

    def grant_repository(self, tenant_id: str, repository: str, auto_fix: bool = False) -> None:
        repository = canonical_repository(repository)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT repository FROM repository_grants "
                "WHERE tenant_id=%s AND LOWER(repository)=%s ORDER BY repository LIMIT 1 FOR UPDATE",
                (tenant_id, repository),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE repository_grants SET auto_fix=%s WHERE tenant_id=%s AND repository=%s",
                    (auto_fix, tenant_id, row["repository"]),
                )
            else:
                conn.execute(
                    "INSERT INTO repository_grants(tenant_id,repository,auto_fix) "
                    "VALUES (%s,%s,%s) ON CONFLICT(tenant_id,repository) "
                    "DO UPDATE SET auto_fix=EXCLUDED.auto_fix",
                    (tenant_id, repository, auto_fix),
                )

    def save_repository_policy(
        self,
        tenant_id: str,
        repository: str,
        policy: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        repository = canonical_repository(repository)
        now = utc_now()
        serialized = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("repository-policy:%s:%s" % (tenant_id, repository),),
            )
            row = conn.execute(
                "SELECT repository,version FROM repository_policies "
                "WHERE tenant_id=%s AND LOWER(repository)=%s "
                "ORDER BY version DESC,updated_at DESC LIMIT 1 FOR UPDATE",
                (tenant_id, repository),
            ).fetchone()
            version = int(row["version"]) + 1 if row else 1
            stored_repository = row["repository"] if row else repository
            conn.execute(
                "INSERT INTO repository_policies(tenant_id,repository,version,enabled,auto_fix,"
                "policy_json,updated_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s) "
                "ON CONFLICT(tenant_id,repository) DO UPDATE SET version=EXCLUDED.version,"
                "enabled=EXCLUDED.enabled,auto_fix=EXCLUDED.auto_fix,"
                "policy_json=EXCLUDED.policy_json,updated_at=EXCLUDED.updated_at",
                (
                    tenant_id,
                    stored_repository,
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
                (tenant_id, stored_repository, version, serialized, actor, now),
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
        repository = canonical_repository(repository)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tenant_id,repository,version,policy_json,updated_at "
                "FROM repository_policies WHERE tenant_id=%s AND LOWER(repository)=%s "
                "ORDER BY version DESC,updated_at DESC LIMIT 1",
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
        repository = canonical_repository(repository)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tenant_id,repository,version,policy_json,actor,created_at "
                "FROM repository_policy_versions WHERE tenant_id=%s AND LOWER(repository)=%s "
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
        repository = canonical_repository(repository)
        with self._connect() as conn:
            policy = conn.execute(
                "SELECT enabled,auto_fix FROM repository_policies "
                "WHERE tenant_id=%s AND LOWER(repository)=%s "
                "ORDER BY version DESC,updated_at DESC LIMIT 1",
                (tenant_id, repository),
            ).fetchone()
            if policy:
                return bool(policy["enabled"] and (not require_auto_fix or policy["auto_fix"]))
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM repository_grants WHERE tenant_id=%s", (tenant_id,)
            ).fetchone()["n"]
            row = conn.execute(
                "SELECT BOOL_OR(auto_fix) AS auto_fix FROM repository_grants "
                "WHERE tenant_id=%s AND LOWER(repository)=%s",
                (tenant_id, repository),
            ).fetchone()
        return (
            True
            if total == 0
            else bool(
                row and row["auto_fix"] is not None and (not require_auto_fix or row["auto_fix"])
            )
        )

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

    def save_deployment(
        self,
        tenant_id: str,
        skill_name: str,
        config: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO deployments(tenant_id,skill_name,stable_version,candidate_version,"
                "canary_percent,shadow_percent,max_error_rate,min_samples,status,samples,errors,"
                "updated_at,generation) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,1) "
                "ON CONFLICT(tenant_id,skill_name) DO UPDATE SET stable_version=EXCLUDED.stable_version,"
                "candidate_version=EXCLUDED.candidate_version,canary_percent=EXCLUDED.canary_percent,"
                "shadow_percent=EXCLUDED.shadow_percent,max_error_rate=EXCLUDED.max_error_rate,"
                "min_samples=EXCLUDED.min_samples,status=EXCLUDED.status,samples=0,errors=0,"
                "updated_at=EXCLUDED.updated_at,generation=deployments.generation+1",
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
            deployment = conn.execute(
                "UPDATE deployments SET max_disagreement_rate=%s,auto_promote=%s,"
                "shadow_samples=0,disagreements=0 WHERE tenant_id=%s AND skill_name=%s "
                "RETURNING *",
                (
                    float(config.get("max_disagreement_rate", 0.2)),
                    bool(config.get("auto_promote", False)),
                    tenant_id,
                    skill_name,
                ),
            ).fetchone()
            if not deployment:
                raise RuntimeError("deployment was not persisted")
            detail = {**config, "generation": int(deployment["generation"])}
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'deployment.configure',%s,%s::jsonb,%s)",
                (
                    tenant_id,
                    actor,
                    skill_name,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
        return dict(deployment)

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
        task_id: str,
        failed: bool,
        candidate_version: int,
        generation: int,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            deployment = conn.execute(
                "SELECT * FROM deployments "
                "WHERE tenant_id=%s AND skill_name=%s AND status='running' "
                "AND candidate_version=%s AND generation=%s FOR UPDATE",
                (tenant_id, skill_name, candidate_version, generation),
            ).fetchone()
            if not deployment:
                return None
            recorded = conn.execute(
                "UPDATE tasks SET input_json=jsonb_set(input_json,'{_release_results}',"
                "COALESCE(input_json->'_release_results','{}'::jsonb)||"
                "jsonb_build_object(%s::text,TRUE)),"
                "updated_at=%s "
                "WHERE id=%s AND tenant_id=%s "
                "AND NOT (COALESCE(input_json->'_release_results','{}'::jsonb) ? %s) RETURNING id",
                (skill_name, utc_now(), task_id, tenant_id, skill_name),
            ).fetchone()
            if not recorded:
                return dict(deployment)
            row = conn.execute(
                "UPDATE deployments SET samples=samples+1,errors=errors+%s,updated_at=%s "
                "WHERE tenant_id=%s AND skill_name=%s RETURNING *",
                (int(failed), utc_now(), tenant_id, skill_name),
            ).fetchone()
            if not row:
                raise RuntimeError("locked deployment disappeared")
            value = dict(row)
            if (
                value["status"] == "running"
                and value["samples"] >= value["min_samples"]
                and value["errors"] / value["samples"] > value["max_error_rate"]
            ):
                now = utc_now()
                conn.execute(
                    "UPDATE deployments SET status='rolled_back',canary_percent=0,shadow_percent=0,"
                    "updated_at=%s WHERE tenant_id=%s AND skill_name=%s",
                    (now, tenant_id, skill_name),
                )
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,'system','deployment.auto-rollback',%s,%s::jsonb,%s)",
                    (
                        tenant_id,
                        skill_name,
                        json.dumps(
                            {
                                "candidate_version": value["candidate_version"],
                                "generation": value["generation"],
                                "samples": value["samples"],
                                "errors": value["errors"],
                                "error_rate": value["errors"] / value["samples"],
                                "max_error_rate": value["max_error_rate"],
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                value.update(status="rolled_back", canary_percent=0, shadow_percent=0)
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
        candidate_version: int,
        generation: int,
        candidate_failed: bool = False,
        audit_event: tuple[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            deployment = conn.execute(
                "SELECT * FROM deployments "
                "WHERE tenant_id=%s AND skill_name=%s AND status='running' "
                "AND candidate_version=%s AND generation=%s FOR UPDATE",
                (tenant_id, skill_name, candidate_version, generation),
            ).fetchone()
            if not deployment:
                return None
            inserted = conn.execute(
                "INSERT INTO release_observations(tenant_id,skill_name,task_id,lane,"
                "primary_json,candidate_json,disagreement,candidate_failed,created_at) "
                "SELECT %s,%s,task.id,%s,%s::jsonb,%s::jsonb,%s,%s,%s "
                "FROM tasks AS task WHERE task.id=%s AND task.tenant_id=%s "
                "ON CONFLICT(tenant_id,skill_name,task_id) DO NOTHING RETURNING id",
                (
                    tenant_id,
                    skill_name,
                    lane,
                    json.dumps(primary, ensure_ascii=False),
                    json.dumps(candidate, ensure_ascii=False) if candidate is not None else None,
                    float(disagreement),
                    candidate_failed,
                    utc_now(),
                    task_id,
                    tenant_id,
                ),
            ).fetchone()
            if not inserted:
                return dict(deployment)
            row = conn.execute(
                "UPDATE deployments SET shadow_samples=shadow_samples+1,"
                "disagreements=disagreements+%s,updated_at=%s "
                "WHERE tenant_id=%s AND skill_name=%s RETURNING *",
                (
                    int(candidate_failed or disagreement > 0),
                    utc_now(),
                    tenant_id,
                    skill_name,
                ),
            ).fetchone()
            if not row:
                raise RuntimeError("locked deployment disappeared")
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
                now = utc_now()
                conn.execute(
                    "UPDATE deployments SET status='promoted',stable_version=candidate_version,"
                    "canary_percent=0,shadow_percent=0,updated_at=%s "
                    "WHERE tenant_id=%s AND skill_name=%s",
                    (now, tenant_id, skill_name),
                )
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,'system','deployment.auto-promote',%s,%s::jsonb,%s)",
                    (
                        tenant_id,
                        skill_name,
                        json.dumps(
                            {
                                "candidate_version": value["candidate_version"],
                                "generation": value["generation"],
                                "shadow_samples": value["shadow_samples"],
                                "disagreements": value["disagreements"],
                                "disagreement_rate": disagreement_rate,
                                "samples": value["samples"],
                                "errors": value["errors"],
                                "error_rate": error_rate,
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                value.update(
                    status="promoted",
                    stable_version=value["candidate_version"],
                    canary_percent=0,
                    shadow_percent=0,
                )
            if audit_event is not None:
                action, detail = audit_event
                if action == "shadow.failed":
                    detail = {
                        "error": preserve_safe_summary(detail.get("error"), "shadow review failed")
                    }
                else:
                    detail = {**detail, "rollout_status": value.get("status")}
                conn.execute(
                    "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                    "VALUES (%s,'system',%s,%s,%s::jsonb,%s)",
                    (
                        tenant_id,
                        action,
                        task_id,
                        json.dumps(detail, ensure_ascii=False),
                        utc_now(),
                    ),
                )
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
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO alerts(tenant_id,alert_key,severity,message,status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'open',%s,%s) "
                "ON CONFLICT(tenant_id,alert_key,status) DO UPDATE SET "
                "severity=EXCLUDED.severity,message=EXCLUDED.message,updated_at=EXCLUDED.updated_at",
                (tenant_id, alert_key, severity, message[:1000], now, now),
            )

    def clear_alert(self, tenant_id: str, alert_key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM alerts WHERE tenant_id=%s AND alert_key=%s AND status='open'",
                (tenant_id, alert_key),
            )
        return cursor.rowcount > 0

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE tenant_id=%s ORDER BY id DESC LIMIT %s",
                (tenant_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def bind_installation(
        self,
        installation_id: int,
        account_login: str,
        tenant_id: str,
        actor: str,
    ) -> None:
        with self._connect() as conn:
            bound = conn.execute(
                "INSERT INTO installations(installation_id,account_login,created_at,tenant_id) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(installation_id) DO UPDATE "
                "SET account_login=EXCLUDED.account_login,created_at=EXCLUDED.created_at,"
                "tenant_id=EXCLUDED.tenant_id WHERE installations.tenant_id=EXCLUDED.tenant_id "
                "RETURNING tenant_id",
                (installation_id, account_login, utc_now(), tenant_id),
            ).fetchone()
            if not bound:
                raise AccessDeniedError("GitHub installation is already bound to another tenant")
            conn.execute(
                "INSERT INTO audit_log(tenant_id,actor,action,resource,detail_json,created_at) "
                "VALUES (%s,%s,'github.installation.bind',%s,%s::jsonb,%s)",
                (
                    tenant_id,
                    actor,
                    str(installation_id),
                    json.dumps({"account": account_login}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def consume_auth_state(self, jti: str, purpose: str, expires_at: int) -> bool:
        with self._connect() as conn:
            consumed = conn.execute(
                "INSERT INTO consumed_auth_states(jti,purpose,expires_at,consumed_at) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(jti) DO NOTHING RETURNING jti",
                (jti, purpose, expires_at, utc_now()),
            ).fetchone()
            conn.execute(
                "DELETE FROM consumed_auth_states "
                "WHERE expires_at < EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)"
            )
        return consumed is not None

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
                "COUNT(*) FILTER(WHERE state='FAILED' AND NOT EXISTS (SELECT 1 FROM "
                "task_admissions AS admission WHERE admission.task_id=tasks.id AND "
                "admission.active=TRUE)) AS failed FROM tasks" + where,
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
        evaluated = row["success"] + row["failed"]
        return {
            "tasks_total": row["total"],
            "tasks_success": row["success"],
            "tasks_failed": row["failed"],
            "success_rate": round(row["success"] / evaluated, 4) if evaluated else 0.0,
            "unresolved_failure_cases": failures,
            "active_skill_versions": skills,
        }


def create_store(
    database_url: str,
    pool_min: int = 1,
    pool_max: int = 10,
    pool_timeout: float = 10.0,
    statement_timeout_seconds: float = 120.0,
    *,
    auto_migrate: bool = False,
) -> PostgresTaskStore:
    if not database_url.startswith(("postgres://", "postgresql://")):
        raise ValueError("EVOAGENT_DATABASE_URL must be a PostgreSQL URL")
    if (
        isinstance(pool_min, bool)
        or isinstance(pool_max, bool)
        or not isinstance(pool_min, int)
        or not isinstance(pool_max, int)
        or pool_min < 0
        or pool_max <= 0
        or pool_min > pool_max
        or pool_max > MAX_PG_POOL_SIZE
    ):
        raise ValueError(
            "EVOAGENT_PG_POOL_MIN/MAX must be integers with 0 <= MIN <= MAX <= %d"
            % MAX_PG_POOL_SIZE
        )
    if not math.isfinite(pool_timeout) or pool_timeout <= 0:
        raise ValueError("EVOAGENT_PG_POOL_TIMEOUT must be positive")
    if not math.isfinite(statement_timeout_seconds) or statement_timeout_seconds <= 0:
        raise ValueError("EVOAGENT_PG_STATEMENT_TIMEOUT_SECONDS must be positive")
    return PostgresTaskStore(
        database_url,
        pool_min,
        pool_max,
        pool_timeout,
        statement_timeout_seconds,
        auto_migrate=auto_migrate,
    )
