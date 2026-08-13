"""PostgreSQL persistence backend.

The implementation mirrors TaskStore's public API and is selected when
EVOAGENT_DATABASE_URL starts with postgres. psycopg is an optional production
dependency so local development can remain zero-config.
"""

import json
import uuid
from contextlib import AbstractContextManager
from typing import Any

from .models import ReviewReport, TaskState, TraceEvent
from .store import utc_now


class PostgresTaskStore:
    def __init__(
        self,
        url: str,
        pool_min: int = 1,
        pool_max: int = 10,
        pool_timeout: float = 10.0,
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
        # A real connection pool avoids a TCP connect + auth handshake on every
        # single query (the previous per-call `psycopg.connect` was the dominant
        # Postgres cost under load). Import-guarded so `psycopg_pool` stays an
        # optional dependency and we fall back to per-call connections.
        self._pool = None
        if pool_max and pool_max > 0:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                print(
                    "WARNING: psycopg_pool not installed; falling back to a new "
                    "connection per query. Install the 'postgres-pool' extra "
                    "(pip install psycopg-pool) for pooled connections."
                )
                ConnectionPool = None
            if ConnectionPool is not None:
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
                        % exc
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
        statements = [
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER, input_json JSONB NOT NULL, report_json JSONB,
                error TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS trace_events (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), step INTEGER NOT NULL,
                state TEXT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS failure_cases (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL, category TEXT NOT NULL,
                payload_json JSONB NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS skill_versions (
                id BIGSERIAL PRIMARY KEY, skill_name TEXT NOT NULL, version INTEGER NOT NULL,
                prompt TEXT NOT NULL, score DOUBLE PRECISION NOT NULL, active BOOLEAN NOT NULL DEFAULT FALSE,
                parent_version INTEGER, created_at TIMESTAMPTZ NOT NULL, UNIQUE(skill_name, version))""",
            """CREATE TABLE IF NOT EXISTS installations (
                installation_id BIGINT PRIMARY KEY, account_login TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evaluation_cases (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, split TEXT NOT NULL,
                diff TEXT NOT NULL, expected_json JSONB NOT NULL, source TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evolution_runs (
                id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL, candidate_score DOUBLE PRECISION NOT NULL,
                baseline_score DOUBLE PRECISION NOT NULL, metrics_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE installations ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            """CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL REFERENCES tasks(id), node TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1, state_json JSONB NOT NULL, error TEXT,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(task_id,node))""",
            """CREATE TABLE IF NOT EXISTS task_payloads (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id), diff TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS agent_messages (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,
                correlation_id TEXT NOT NULL, content_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, task_id TEXT, received_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL REFERENCES users(id), tenant_id TEXT NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY(user_id,tenant_id))""",
            """CREATE TABLE IF NOT EXISTS repository_grants (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL, auto_fix BOOLEAN NOT NULL DEFAULT FALSE,
                PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, resource TEXT NOT NULL, detail_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS deployments (
                tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL, stable_version INTEGER,
                candidate_version INTEGER, canary_percent INTEGER NOT NULL DEFAULT 0,
                shadow_percent INTEGER NOT NULL DEFAULT 0, max_error_rate DOUBLE PRECISION NOT NULL DEFAULT .1,
                min_samples INTEGER NOT NULL DEFAULT 20, status TEXT NOT NULL DEFAULT 'stable',
                samples INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(tenant_id,skill_name))""",
            """CREATE TABLE IF NOT EXISTS alerts (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, alert_key TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,alert_key,status))""",
            """CREATE TABLE IF NOT EXISTS review_sessions (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                latest_head_sha TEXT, pending_input TEXT, created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,repository,pull_request))""",
            """CREATE TABLE IF NOT EXISTS session_turns (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
                task_id TEXT, head_sha TEXT, trigger TEXT NOT NULL, sequence INTEGER NOT NULL,
                summary_json JSONB, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(session_id, sequence))""",
            """CREATE TABLE IF NOT EXISTS session_findings (
                id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, snapshot_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_session_turns_session "
            "ON session_turns(session_id, sequence)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_turn ON session_findings(turn_id)",
        ]
        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

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
        with self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET state=%s,error=%s,updated_at=%s WHERE id=%s",
                (TaskState.FAILED.value, error[:2000], event.created_at, task_id),
            )
            conn.execute(
                "INSERT INTO trace_events(task_id,step,state,message,created_at) VALUES (%s,%s,%s,%s,%s)",
                (task_id, event.step, event.state.value, event.message, event.created_at),
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
                    json.dumps(message.get("content", {}), ensure_ascii=False),
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
        now = utc_now()
        with self._connect() as conn:
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

    def repository_allowed(
        self,
        tenant_id: str,
        repository: str,
        require_auto_fix: bool = False,
    ) -> bool:
        with self._connect() as conn:
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

    def create_alert(
        self,
        tenant_id: str,
        alert_key: str,
        severity: str,
        message: str,
    ) -> None:
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
):
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresTaskStore(database_url, pool_min, pool_max, pool_timeout)
    from .store import TaskStore

    return TaskStore(sqlite_path)
