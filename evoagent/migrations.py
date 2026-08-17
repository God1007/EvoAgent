"""Versioned, checksummed schema migrations for SQLite and PostgreSQL.

Migration definitions are immutable once released.  Both adapters share the
same logical version history while retaining dialect-specific SQL.  Existing
pre-migration databases are adopted by replaying idempotent migrations and
recording their history; newer or tampered histories fail closed at startup.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class SchemaMigrationError(RuntimeError):
    """Base class for startup failures caused by database schema state."""


class SchemaTooNewError(SchemaMigrationError):
    """The database was migrated by a newer application release."""


class SchemaHistoryError(SchemaMigrationError):
    """The recorded migration history is incomplete or has been modified."""


class MigrationApplyError(SchemaMigrationError):
    """A pending migration could not be applied transactionally."""


@dataclass(frozen=True)
class SQLiteColumn:
    table: str
    name: str
    declaration: str


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite_statements: tuple[str, ...]
    postgres_statements: tuple[str, ...]
    sqlite_columns: tuple[SQLiteColumn, ...] = ()

    @property
    def checksum(self) -> str:
        payload = {
            "version": self.version,
            "name": self.name,
            "sqlite": self.sqlite_statements,
            "postgres": self.postgres_statements,
            "sqlite_columns": [
                (column.table, column.name, column.declaration) for column in self.sqlite_columns
            ],
        }
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "task-runtime",
        (
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER, input_json TEXT NOT NULL, report_json TEXT,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                cancel_requested INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                step INTEGER NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
                created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id))""",
            """CREATE TABLE IF NOT EXISTS failure_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                category TEXT NOT NULL, payload_json TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS installations (
                installation_id INTEGER PRIMARY KEY, account_login TEXT NOT NULL,
                created_at TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT 'default')""",
            """CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL, node TEXT NOT NULL, status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1, state_json TEXT NOT NULL, error TEXT,
                updated_at TEXT NOT NULL, PRIMARY KEY(task_id,node),
                FOREIGN KEY(task_id) REFERENCES tasks(id))""",
            """CREATE TABLE IF NOT EXISTS task_payloads (
                task_id TEXT PRIMARY KEY, diff TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id))""",
            """CREATE TABLE IF NOT EXISTS agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,
                correlation_id TEXT NOT NULL, content_json TEXT NOT NULL,
                created_at TEXT NOT NULL, FOREIGN KEY(task_id) REFERENCES tasks(id))""",
            """CREATE TABLE IF NOT EXISTS webhook_deliveries (
                delivery_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, task_id TEXT, received_at TEXT NOT NULL)""",
        ),
        (
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER, input_json JSONB NOT NULL, report_json JSONB,
                error TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                cancel_requested BOOLEAN NOT NULL DEFAULT FALSE)""",
            """CREATE TABLE IF NOT EXISTS trace_events (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                step INTEGER NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS failure_cases (
                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL, category TEXT NOT NULL,
                payload_json JSONB NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS installations (
                installation_id BIGINT PRIMARY KEY, account_login TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                tenant_id TEXT NOT NULL DEFAULT 'default')""",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS "
            "cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE installations ADD COLUMN IF NOT EXISTS "
            "tenant_id TEXT NOT NULL DEFAULT 'default'",
            """CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL REFERENCES tasks(id), node TEXT NOT NULL,
                status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                state_json JSONB NOT NULL, error TEXT, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(task_id,node))""",
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
        ),
        (
            SQLiteColumn("tasks", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"),
            SQLiteColumn("tasks", "cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
            SQLiteColumn("installations", "tenant_id", "TEXT NOT NULL DEFAULT 'default'"),
        ),
    ),
    Migration(
        2,
        "governance-and-evolution",
        (
            """CREATE TABLE IF NOT EXISTS skill_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, skill_name TEXT NOT NULL,
                version INTEGER NOT NULL, prompt TEXT NOT NULL, score REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 0, parent_version INTEGER,
                created_at TEXT NOT NULL, UNIQUE(skill_name,version))""",
            """CREATE TABLE IF NOT EXISTS evaluation_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                split TEXT NOT NULL, diff TEXT NOT NULL, expected_json TEXT NOT NULL,
                source TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evolution_runs (
                id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL, candidate_score REAL NOT NULL,
                baseline_score REAL NOT NULL, metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, role TEXT NOT NULL,
                PRIMARY KEY(user_id,tenant_id), FOREIGN KEY(user_id) REFERENCES users(id))""",
            """CREATE TABLE IF NOT EXISTS repository_grants (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                auto_fix INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
                detail_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS deployments (
                tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL, stable_version INTEGER,
                candidate_version INTEGER, canary_percent INTEGER NOT NULL DEFAULT 0,
                shadow_percent INTEGER NOT NULL DEFAULT 0,
                max_error_rate REAL NOT NULL DEFAULT 0.1, min_samples INTEGER NOT NULL DEFAULT 20,
                status TEXT NOT NULL DEFAULT 'stable', samples INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,skill_name))""",
            """CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                alert_key TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, UNIQUE(tenant_id,alert_key,status))""",
        ),
        (
            """CREATE TABLE IF NOT EXISTS skill_versions (
                id BIGSERIAL PRIMARY KEY, skill_name TEXT NOT NULL, version INTEGER NOT NULL,
                prompt TEXT NOT NULL, score DOUBLE PRECISION NOT NULL,
                active BOOLEAN NOT NULL DEFAULT FALSE, parent_version INTEGER,
                created_at TIMESTAMPTZ NOT NULL, UNIQUE(skill_name,version))""",
            """CREATE TABLE IF NOT EXISTS evaluation_cases (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, split TEXT NOT NULL,
                diff TEXT NOT NULL, expected_json JSONB NOT NULL, source TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS evolution_runs (
                id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, candidate_version INTEGER NOT NULL,
                baseline_version INTEGER, decision TEXT NOT NULL,
                candidate_score DOUBLE PRECISION NOT NULL,
                baseline_score DOUBLE PRECISION NOT NULL, metrics_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL REFERENCES users(id), tenant_id TEXT NOT NULL,
                role TEXT NOT NULL, PRIMARY KEY(user_id,tenant_id))""",
            """CREATE TABLE IF NOT EXISTS repository_grants (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                auto_fix BOOLEAN NOT NULL DEFAULT FALSE, PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, resource TEXT NOT NULL, detail_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS deployments (
                tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL, stable_version INTEGER,
                candidate_version INTEGER, canary_percent INTEGER NOT NULL DEFAULT 0,
                shadow_percent INTEGER NOT NULL DEFAULT 0,
                max_error_rate DOUBLE PRECISION NOT NULL DEFAULT .1,
                min_samples INTEGER NOT NULL DEFAULT 20,
                status TEXT NOT NULL DEFAULT 'stable', samples INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,skill_name))""",
            """CREATE TABLE IF NOT EXISTS alerts (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, alert_key TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,alert_key,status))""",
        ),
    ),
    Migration(
        3,
        "sessions-and-shadow-release",
        (
            """CREATE TABLE IF NOT EXISTS release_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                skill_name TEXT NOT NULL, task_id TEXT NOT NULL, lane TEXT NOT NULL,
                primary_json TEXT NOT NULL, candidate_json TEXT,
                disagreement REAL NOT NULL, candidate_failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS review_sessions (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                latest_head_sha TEXT, pending_input TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, UNIQUE(tenant_id,repository,pull_request))""",
            """CREATE TABLE IF NOT EXISTS session_turns (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT, head_sha TEXT,
                trigger TEXT NOT NULL, sequence INTEGER NOT NULL, summary_json TEXT,
                created_at TEXT NOT NULL, UNIQUE(session_id,sequence),
                FOREIGN KEY(session_id) REFERENCES review_sessions(id))""",
            """CREATE TABLE IF NOT EXISTS session_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(turn_id) REFERENCES session_turns(id))""",
            "CREATE INDEX IF NOT EXISTS idx_release_observations_deployment "
            "ON release_observations(tenant_id,skill_name,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_session_turns_session "
            "ON session_turns(session_id,sequence)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_turn ON session_findings(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id,created_at)",
        ),
        (
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
            "max_disagreement_rate DOUBLE PRECISION NOT NULL DEFAULT .2",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
            "auto_promote BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
            "shadow_samples INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS "
            "disagreements INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS release_observations (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL,
                task_id TEXT NOT NULL, lane TEXT NOT NULL, primary_json JSONB NOT NULL,
                candidate_json JSONB, disagreement DOUBLE PRECISION NOT NULL,
                candidate_failed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS review_sessions (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,
                pull_request INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                latest_head_sha TEXT, pending_input TEXT, created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,repository,pull_request))""",
            """CREATE TABLE IF NOT EXISTS session_turns (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES review_sessions(id),
                task_id TEXT, head_sha TEXT, trigger TEXT NOT NULL, sequence INTEGER NOT NULL,
                summary_json JSONB, created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(session_id,sequence))""",
            """CREATE TABLE IF NOT EXISTS session_findings (
                id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, status TEXT NOT NULL, snapshot_json JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_release_observations_deployment "
            "ON release_observations(tenant_id,skill_name,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_session_turns_session "
            "ON session_turns(session_id,sequence)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_turn ON session_findings(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id,created_at)",
        ),
        (
            SQLiteColumn("deployments", "max_disagreement_rate", "REAL NOT NULL DEFAULT 0.2"),
            SQLiteColumn("deployments", "auto_promote", "INTEGER NOT NULL DEFAULT 0"),
            SQLiteColumn("deployments", "shadow_samples", "INTEGER NOT NULL DEFAULT 0"),
            SQLiteColumn("deployments", "disagreements", "INTEGER NOT NULL DEFAULT 0"),
        ),
    ),
    Migration(
        4,
        "transactional-outbox-and-effects",
        (
            """CREATE TABLE IF NOT EXISTS outbox_messages (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, message_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
                lease_owner TEXT, lease_until TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, published_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS effect_receipts (
                effect_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                result_json TEXT, last_error TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, completed_at TEXT)""",
            "CREATE INDEX IF NOT EXISTS idx_outbox_dispatch "
            "ON outbox_messages(status,available_at,lease_until)",
        ),
        (
            """CREATE TABLE IF NOT EXISTS outbox_messages (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, message_key TEXT NOT NULL UNIQUE,
                payload_json JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL,
                lease_owner TEXT, lease_until TIMESTAMPTZ, last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                published_at TIMESTAMPTZ)""",
            """CREATE TABLE IF NOT EXISTS effect_receipts (
                effect_key TEXT PRIMARY KEY, status TEXT NOT NULL,
                owner TEXT, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
                result_json JSONB, last_error TEXT, created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ)""",
            "CREATE INDEX IF NOT EXISTS idx_outbox_dispatch "
            "ON outbox_messages(status,available_at,lease_until)",
        ),
    ),
    Migration(
        5,
        "versioned-repository-policies",
        (
            """CREATE TABLE IF NOT EXISTS repository_policies (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL, version INTEGER NOT NULL,
                enabled INTEGER NOT NULL, auto_fix INTEGER NOT NULL,
                policy_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS repository_policy_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, version INTEGER NOT NULL,
                policy_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(tenant_id,repository,version))""",
            "CREATE INDEX IF NOT EXISTS idx_repository_policy_versions "
            "ON repository_policy_versions(tenant_id,repository,version DESC)",
        ),
        (
            """CREATE TABLE IF NOT EXISTS repository_policies (
                tenant_id TEXT NOT NULL, repository TEXT NOT NULL, version INTEGER NOT NULL,
                enabled BOOLEAN NOT NULL, auto_fix BOOLEAN NOT NULL,
                policy_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,repository))""",
            """CREATE TABLE IF NOT EXISTS repository_policy_versions (
                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, version INTEGER NOT NULL,
                policy_json JSONB NOT NULL, actor TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,repository,version))""",
            "CREATE INDEX IF NOT EXISTS idx_repository_policy_versions "
            "ON repository_policy_versions(tenant_id,repository,version DESC)",
        ),
    ),
    Migration(
        6,
        "model-gateway-usage-ledger",
        (
            """CREATE TABLE IF NOT EXISTS model_usage (
                request_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                reserved_tokens INTEGER NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reserved_cost_micros INTEGER NOT NULL DEFAULT 0,
                cost_micros INTEGER NOT NULL DEFAULT 0, redactions INTEGER NOT NULL DEFAULT 0,
                request_sha256 TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                completed_at TEXT)""",
            "CREATE INDEX IF NOT EXISTS idx_model_usage_budget "
            "ON model_usage(tenant_id,repository,created_at,status)",
            "CREATE INDEX IF NOT EXISTS idx_model_usage_task ON model_usage(task_id,created_at)",
        ),
        (
            """CREATE TABLE IF NOT EXISTS model_usage (
                request_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                reserved_tokens BIGINT NOT NULL, input_tokens BIGINT NOT NULL DEFAULT 0,
                output_tokens BIGINT NOT NULL DEFAULT 0,
                reserved_cost_micros BIGINT NOT NULL DEFAULT 0,
                cost_micros BIGINT NOT NULL DEFAULT 0, redactions INTEGER NOT NULL DEFAULT 0,
                request_sha256 TEXT NOT NULL, error TEXT, created_at TIMESTAMPTZ NOT NULL,
                completed_at TIMESTAMPTZ)""",
            "CREATE INDEX IF NOT EXISTS idx_model_usage_budget "
            "ON model_usage(tenant_id,repository,created_at,status)",
            "CREATE INDEX IF NOT EXISTS idx_model_usage_task ON model_usage(task_id,created_at)",
        ),
    ),
    Migration(
        7,
        "model-route-attempt-correlation",
        (),
        (
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS root_request_id TEXT",
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS route_id TEXT",
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1",
        ),
        (
            SQLiteColumn("model_usage", "root_request_id", "TEXT"),
            SQLiteColumn("model_usage", "route_id", "TEXT"),
            SQLiteColumn("model_usage", "attempt", "INTEGER NOT NULL DEFAULT 1"),
        ),
    ),
    Migration(
        8,
        "queue-recovery-operations",
        (
            "CREATE INDEX IF NOT EXISTS idx_tasks_recovery "
            "ON tasks(cancel_requested,state,created_at,id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_recovery_epoch "
            "ON audit_log(action,resource) "
            "WHERE tenant_id='system' AND action='recovery.queue.stage'",
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_tasks_recovery "
            "ON tasks(cancel_requested,state,created_at,id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_recovery_epoch "
            "ON audit_log(action,resource) "
            "WHERE tenant_id='system' AND action='recovery.queue.stage'",
        ),
    ),
    Migration(
        9,
        "model-usage-reconciliation",
        (
            "CREATE INDEX IF NOT EXISTS idx_model_usage_reconciliation "
            "ON model_usage(status,created_at)",
        ),
        (
            "CREATE INDEX IF NOT EXISTS idx_model_usage_reconciliation "
            "ON model_usage(status,created_at)",
        ),
    ),
    Migration(
        10,
        "sanitize-legacy-operational-errors",
        (
            "UPDATE tasks SET error='review execution failed "
            "[type=legacy; ref=0000000000000000]' WHERE error IS NOT NULL AND error<>''",
            "UPDATE trace_events SET message='review execution failed "
            "[type=legacy; ref=0000000000000000]' WHERE state='FAILED'",
            "UPDATE checkpoints SET error='review node failed "
            "[type=legacy; ref=0000000000000000]' WHERE error IS NOT NULL AND error<>''",
            'UPDATE failure_cases SET payload_json=\'{"error":"review execution failed '
            "[type=legacy; ref=0000000000000000]\"}' WHERE category='execution_error'",
            'UPDATE agent_messages SET content_json=\'{"error":"review agent failed '
            "[type=legacy; ref=0000000000000000]\"}' WHERE kind='agent_failure'",
            "UPDATE outbox_messages SET last_error='outbox dispatch failed "
            "[type=legacy; ref=0000000000000000]' "
            "WHERE last_error IS NOT NULL AND last_error<>''",
            "UPDATE effect_receipts SET last_error='external effect failed "
            "[type=legacy; ref=0000000000000000]' "
            "WHERE last_error IS NOT NULL AND last_error<>''",
            "UPDATE alerts SET message='task delivery failed "
            "[type=legacy; ref=0000000000000000]' WHERE alert_key LIKE 'dlq:%'",
            'UPDATE audit_log SET detail_json=\'{"error":"shadow review failed '
            "[type=legacy; ref=0000000000000000]\"}' WHERE action='shadow.failed'",
        ),
        (
            "UPDATE tasks SET error='review execution failed "
            "[type=legacy; ref=0000000000000000]' WHERE error IS NOT NULL AND error<>''",
            "UPDATE trace_events SET message='review execution failed "
            "[type=legacy; ref=0000000000000000]' WHERE state='FAILED'",
            "UPDATE checkpoints SET error='review node failed "
            "[type=legacy; ref=0000000000000000]' WHERE error IS NOT NULL AND error<>''",
            'UPDATE failure_cases SET payload_json=\'{"error":"review execution failed '
            "[type=legacy; ref=0000000000000000]\"}'::jsonb "
            "WHERE category='execution_error'",
            'UPDATE agent_messages SET content_json=\'{"error":"review agent failed '
            "[type=legacy; ref=0000000000000000]\"}'::jsonb WHERE kind='agent_failure'",
            "UPDATE outbox_messages SET last_error='outbox dispatch failed "
            "[type=legacy; ref=0000000000000000]' "
            "WHERE last_error IS NOT NULL AND last_error<>''",
            "UPDATE effect_receipts SET last_error='external effect failed "
            "[type=legacy; ref=0000000000000000]' "
            "WHERE last_error IS NOT NULL AND last_error<>''",
            "UPDATE alerts SET message='task delivery failed "
            "[type=legacy; ref=0000000000000000]' WHERE alert_key LIKE 'dlq:%'",
            'UPDATE audit_log SET detail_json=\'{"error":"shadow review failed '
            "[type=legacy; ref=0000000000000000]\"}'::jsonb WHERE action='shadow.failed'",
        ),
    ),
    Migration(
        11,
        "model-route-shadow-governance",
        (
            """CREATE TABLE IF NOT EXISTS model_route_shadows (
                observation_id TEXT PRIMARY KEY, topology_sha256 TEXT NOT NULL,
                root_request_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL,
                active_route_id TEXT NOT NULL, candidate_route_id TEXT NOT NULL,
                status TEXT NOT NULL, agreement INTEGER,
                active_output_sha256 TEXT NOT NULL, candidate_output_sha256 TEXT,
                input_sha256 TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_micros INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0, error_type TEXT, error_ref TEXT,
                created_at TEXT NOT NULL, completed_at TEXT)""",
            "CREATE INDEX IF NOT EXISTS idx_model_route_shadows_report "
            "ON model_route_shadows(tenant_id,candidate_route_id,topology_sha256,created_at)",
        ),
        (
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS topology_sha256 TEXT",
            """CREATE TABLE IF NOT EXISTS model_route_shadows (
                observation_id TEXT PRIMARY KEY, topology_sha256 TEXT NOT NULL,
                root_request_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                repository TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL,
                active_route_id TEXT NOT NULL, candidate_route_id TEXT NOT NULL,
                status TEXT NOT NULL, agreement BOOLEAN,
                active_output_sha256 TEXT NOT NULL, candidate_output_sha256 TEXT,
                input_sha256 TEXT NOT NULL, input_tokens BIGINT NOT NULL DEFAULT 0,
                output_tokens BIGINT NOT NULL DEFAULT 0,
                cost_micros BIGINT NOT NULL DEFAULT 0,
                duration_ms BIGINT NOT NULL DEFAULT 0, error_type TEXT, error_ref TEXT,
                created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ)""",
            "CREATE INDEX IF NOT EXISTS idx_model_route_shadows_report "
            "ON model_route_shadows(tenant_id,candidate_route_id,topology_sha256,created_at)",
        ),
        (
            SQLiteColumn("model_usage", "lane", "TEXT NOT NULL DEFAULT 'active'"),
            SQLiteColumn("model_usage", "topology_sha256", "TEXT"),
        ),
    ),
    Migration(
        12,
        "distributed-model-route-capacity",
        (
            """CREATE TABLE IF NOT EXISTS model_route_capacity_leases (
                lease_id TEXT PRIMARY KEY, topology_sha256 TEXT NOT NULL,
                route_id TEXT NOT NULL, root_request_id TEXT NOT NULL,
                expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_model_route_capacity_leases "
            "ON model_route_capacity_leases(route_id,expires_at)",
            """CREATE TABLE IF NOT EXISTS model_route_capacity_windows (
                topology_sha256 TEXT NOT NULL, route_id TEXT NOT NULL,
                window_start TEXT NOT NULL, admitted INTEGER NOT NULL DEFAULT 0,
                concurrency_rejections INTEGER NOT NULL DEFAULT 0,
                rate_rejections INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                PRIMARY KEY(topology_sha256,route_id,window_start))""",
            "CREATE INDEX IF NOT EXISTS idx_model_route_capacity_windows "
            "ON model_route_capacity_windows(route_id,window_start)",
        ),
        (
            """CREATE TABLE IF NOT EXISTS model_route_capacity_leases (
                lease_id TEXT PRIMARY KEY, topology_sha256 TEXT NOT NULL,
                route_id TEXT NOT NULL, root_request_id TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_model_route_capacity_leases "
            "ON model_route_capacity_leases(route_id,expires_at)",
            """CREATE TABLE IF NOT EXISTS model_route_capacity_windows (
                topology_sha256 TEXT NOT NULL, route_id TEXT NOT NULL,
                window_start TIMESTAMPTZ NOT NULL, admitted BIGINT NOT NULL DEFAULT 0,
                concurrency_rejections BIGINT NOT NULL DEFAULT 0,
                rate_rejections BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(topology_sha256,route_id,window_start))""",
            "CREATE INDEX IF NOT EXISTS idx_model_route_capacity_windows "
            "ON model_route_capacity_windows(route_id,window_start)",
        ),
    ),
    Migration(
        13,
        "operational-history-retention",
        (
            "CREATE INDEX IF NOT EXISTS idx_trace_events_retention ON trace_events(created_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_retention "
            "ON session_findings(created_at,id)",
        ),
        (
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS trace_pruned_at TIMESTAMPTZ",
            "ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS findings_pruned_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS idx_trace_events_retention ON trace_events(created_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_retention "
            "ON session_findings(created_at,id)",
        ),
        (
            SQLiteColumn("tasks", "trace_pruned_at", "TEXT"),
            SQLiteColumn("session_turns", "findings_pruned_at", "TEXT"),
        ),
    ),
)

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version
MIN_SUPPORTED_SCHEMA_VERSION = 0
_MIGRATION_BY_VERSION = {migration.version: migration for migration in MIGRATIONS}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_target(target_version: int) -> None:
    if not MIN_SUPPORTED_SCHEMA_VERSION <= target_version <= CURRENT_SCHEMA_VERSION:
        raise ValueError(
            "target schema version must be between %d and %d"
            % (MIN_SUPPORTED_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)
        )


def _validate_history(rows: list[Any], target_version: int) -> set[int]:
    if not rows:
        return set()
    versions = [int(row["version"]) for row in rows]
    newest = max(versions)
    if newest > CURRENT_SCHEMA_VERSION:
        raise SchemaTooNewError(
            "database schema version %d is newer than supported version %d; "
            "deploy a compatible EvoAgent release instead of starting this binary"
            % (newest, CURRENT_SCHEMA_VERSION)
        )
    expected = list(range(1, newest + 1))
    if versions != expected:
        raise SchemaHistoryError(
            "database migration history is not contiguous: expected %s, found %s"
            % (expected, versions)
        )
    for row in rows:
        migration = _MIGRATION_BY_VERSION.get(int(row["version"]))
        if migration is None:
            raise SchemaHistoryError("unknown schema migration version %s" % row["version"])
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise SchemaHistoryError(
                "schema migration %d (%s) does not match the immutable application history"
                % (migration.version, migration.name)
            )
    if newest > target_version:
        raise SchemaTooNewError(
            "database schema version %d is newer than requested target %d"
            % (newest, target_version)
        )
    return set(versions)


def validate_current_schema_history(rows: list[Any]) -> int:
    """Read-only gate for operational tools that must never migrate a database."""
    applied = _validate_history(rows, CURRENT_SCHEMA_VERSION)
    expected = set(range(1, CURRENT_SCHEMA_VERSION + 1))
    if applied != expected:
        raise SchemaHistoryError(
            "database schema is not at required version %d" % CURRENT_SCHEMA_VERSION
        )
    return CURRENT_SCHEMA_VERSION


def _ensure_sqlite_column(conn: sqlite3.Connection, column: SQLiteColumn) -> None:
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % column.table).fetchall()
    }
    if column.name not in columns:
        conn.execute(
            "ALTER TABLE %s ADD COLUMN %s %s" % (column.table, column.name, column.declaration)
        )


def migrate_sqlite(conn: sqlite3.Connection, target_version: int = CURRENT_SCHEMA_VERSION) -> int:
    """Apply all pending SQLite migrations under one immediate transaction."""
    _validate_target(target_version)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL)"""
        )
        rows = conn.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = _validate_history(list(rows), target_version)
        for migration in MIGRATIONS:
            if migration.version > target_version or migration.version in applied:
                continue
            for statement in migration.sqlite_statements:
                conn.execute(statement)
            for column in migration.sqlite_columns:
                _ensure_sqlite_column(conn, column)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (migration.version, migration.name, migration.checksum, _now()),
            )
        conn.commit()
    except SchemaMigrationError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise MigrationApplyError("SQLite schema migration failed: %s" % exc) from exc
    return target_version


def migrate_postgres(conn: Any, target_version: int = CURRENT_SCHEMA_VERSION) -> int:
    """Apply pending PostgreSQL migrations in the caller-owned transaction."""
    _validate_target(target_version)
    try:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("evoagent:schema-migrations",),
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL)"""
        )
        rows = conn.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        applied = _validate_history(list(rows), target_version)
        for migration in MIGRATIONS:
            if migration.version > target_version or migration.version in applied:
                continue
            for statement in migration.postgres_statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (%s,%s,%s,%s)",
                (migration.version, migration.name, migration.checksum, _now()),
            )
    except SchemaMigrationError:
        raise
    except Exception as exc:
        raise MigrationApplyError("PostgreSQL schema migration failed: %s" % exc) from exc
    return target_version
