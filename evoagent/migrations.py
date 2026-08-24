"""Versioned, checksummed PostgreSQL schema migrations."""

from __future__ import annotations

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
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "task-runtime",
        (
            "CREATE TABLE IF NOT EXISTS tasks (\n"
            "                id TEXT PRIMARY KEY, state TEXT NOT NULL, repository TEXT NOT NULL,\n"
            "                pull_request INTEGER, input_json JSONB NOT NULL, report_json JSONB,\n"
            "                error TEXT, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT "
            "NULL,\n"
            "                tenant_id TEXT NOT NULL DEFAULT 'default',\n"
            "                cancel_requested BOOLEAN NOT NULL DEFAULT FALSE)",
            "CREATE TABLE IF NOT EXISTS trace_events (\n"
            "                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),\n"
            "                step INTEGER NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS failure_cases (\n"
            "                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL, category TEXT NOT NULL,\n"
            "                payload_json JSONB NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS installations (\n"
            "                installation_id BIGINT PRIMARY KEY, account_login TEXT NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL,\n"
            "                tenant_id TEXT NOT NULL DEFAULT 'default')",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE installations ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default'",
            "CREATE TABLE IF NOT EXISTS checkpoints (\n"
            "                task_id TEXT NOT NULL REFERENCES tasks(id), node TEXT NOT NULL,\n"
            "                status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,\n"
            "                state_json JSONB NOT NULL, error TEXT, updated_at TIMESTAMPTZ NOT NULL,\n"
            "                PRIMARY KEY(task_id,node))",
            "CREATE TABLE IF NOT EXISTS task_payloads (\n"
            "                task_id TEXT PRIMARY KEY REFERENCES tasks(id), diff TEXT NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS agent_messages (\n"
            "                id BIGSERIAL PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),\n"
            "                sender TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL,\n"
            "                correlation_id TEXT NOT NULL, content_json JSONB NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS webhook_deliveries (\n"
            "                delivery_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, event_type TEXT NOT "
            "NULL,\n"
            "                payload_sha256 TEXT NOT NULL, task_id TEXT, received_at TIMESTAMPTZ NOT "
            "NULL)",
        ),
        "6b93dc2730adc5ff530fe57e1d55689f4587e9f03c711d07f00c73399aec19b0",
    ),
    Migration(
        2,
        "governance-and-evolution",
        (
            "CREATE TABLE IF NOT EXISTS skill_versions (\n"
            "                id BIGSERIAL PRIMARY KEY, skill_name TEXT NOT NULL, version INTEGER NOT "
            "NULL,\n"
            "                prompt TEXT NOT NULL, score DOUBLE PRECISION NOT NULL,\n"
            "                active BOOLEAN NOT NULL DEFAULT FALSE, parent_version INTEGER,\n"
            "                created_at TIMESTAMPTZ NOT NULL, UNIQUE(skill_name,version))",
            "CREATE TABLE IF NOT EXISTS evaluation_cases (\n"
            "                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, split TEXT NOT NULL,\n"
            "                diff TEXT NOT NULL, expected_json JSONB NOT NULL, source TEXT NOT NULL,\n"
            "                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS evolution_runs (\n"
            "                id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, candidate_version INTEGER NOT "
            "NULL,\n"
            "                baseline_version INTEGER, decision TEXT NOT NULL,\n"
            "                candidate_score DOUBLE PRECISION NOT NULL,\n"
            "                baseline_score DOUBLE PRECISION NOT NULL, metrics_json JSONB NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS users (\n"
            "                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,\n"
            "                password_hash TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memberships (\n"
            "                user_id TEXT NOT NULL REFERENCES users(id), tenant_id TEXT NOT NULL,\n"
            "                role TEXT NOT NULL, PRIMARY KEY(user_id,tenant_id))",
            "CREATE TABLE IF NOT EXISTS repository_grants (\n"
            "                tenant_id TEXT NOT NULL, repository TEXT NOT NULL,\n"
            "                auto_fix BOOLEAN NOT NULL DEFAULT FALSE, PRIMARY KEY(tenant_id,repository))",
            "CREATE TABLE IF NOT EXISTS audit_log (\n"
            "                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, actor TEXT NOT NULL,\n"
            "                action TEXT NOT NULL, resource TEXT NOT NULL, detail_json JSONB NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS deployments (\n"
            "                tenant_id TEXT NOT NULL, skill_name TEXT NOT NULL, stable_version INTEGER,\n"
            "                candidate_version INTEGER, canary_percent INTEGER NOT NULL DEFAULT 0,\n"
            "                shadow_percent INTEGER NOT NULL DEFAULT 0,\n"
            "                max_error_rate DOUBLE PRECISION NOT NULL DEFAULT .1,\n"
            "                min_samples INTEGER NOT NULL DEFAULT 20,\n"
            "                status TEXT NOT NULL DEFAULT 'stable', samples INTEGER NOT NULL DEFAULT 0,\n"
            "                errors INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL,\n"
            "                PRIMARY KEY(tenant_id,skill_name))",
            "CREATE TABLE IF NOT EXISTS alerts (\n"
            "                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, alert_key TEXT NOT NULL,\n"
            "                severity TEXT NOT NULL, message TEXT NOT NULL,\n"
            "                status TEXT NOT NULL DEFAULT 'open', created_at TIMESTAMPTZ NOT NULL,\n"
            "                updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,alert_key,status))",
        ),
        "b8be97d864aba2cacfce5a010acef7d9c96e3cad9f8efc0359ea54a306ede859",
    ),
    Migration(
        3,
        "sessions-and-shadow-release",
        (
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS max_disagreement_rate DOUBLE PRECISION NOT "
            "NULL DEFAULT .2",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS auto_promote BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS shadow_samples INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE deployments ADD COLUMN IF NOT EXISTS disagreements INTEGER NOT NULL DEFAULT 0",
            "CREATE TABLE IF NOT EXISTS release_observations (\n"
            "                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, skill_name TEXT NOT "
            "NULL,\n"
            "                task_id TEXT NOT NULL, lane TEXT NOT NULL, primary_json JSONB NOT NULL,\n"
            "                candidate_json JSONB, disagreement DOUBLE PRECISION NOT NULL,\n"
            "                candidate_failed BOOLEAN NOT NULL DEFAULT FALSE,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE TABLE IF NOT EXISTS review_sessions (\n"
            "                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, repository TEXT NOT NULL,\n"
            "                pull_request INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open',\n"
            "                latest_head_sha TEXT, pending_input TEXT, created_at TIMESTAMPTZ NOT NULL,\n"
            "                updated_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,repository,pull_request))",
            "CREATE TABLE IF NOT EXISTS session_turns (\n"
            "                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES "
            "review_sessions(id),\n"
            "                task_id TEXT, head_sha TEXT, trigger TEXT NOT NULL, sequence INTEGER NOT "
            "NULL,\n"
            "                summary_json JSONB, created_at TIMESTAMPTZ NOT NULL,\n"
            "                UNIQUE(session_id,sequence))",
            "CREATE TABLE IF NOT EXISTS session_findings (\n"
            "                id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,\n"
            "                fingerprint TEXT NOT NULL, status TEXT NOT NULL, snapshot_json JSONB NOT "
            "NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_release_observations_deployment ON "
            "release_observations(tenant_id,skill_name,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_session_turns_session ON session_turns(session_id,sequence)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_turn ON session_findings(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_tasks_tenant_created ON tasks(tenant_id,created_at)",
        ),
        "77b72ccdbed5ef2e06040d322b8c2327673716c4838eba819188aec6278d5aeb",
    ),
    Migration(
        4,
        "transactional-outbox-and-effects",
        (
            "CREATE TABLE IF NOT EXISTS outbox_messages (\n"
            "                id TEXT PRIMARY KEY, topic TEXT NOT NULL, message_key TEXT NOT NULL UNIQUE,\n"
            "                payload_json JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'pending',\n"
            "                attempts INTEGER NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL,\n"
            "                lease_owner TEXT, lease_until TIMESTAMPTZ, last_error TEXT,\n"
            "                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,\n"
            "                published_at TIMESTAMPTZ)",
            "CREATE TABLE IF NOT EXISTS effect_receipts (\n"
            "                effect_key TEXT PRIMARY KEY, status TEXT NOT NULL,\n"
            "                owner TEXT, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,\n"
            "                result_json JSONB, last_error TEXT, created_at TIMESTAMPTZ NOT NULL,\n"
            "                updated_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ)",
            "CREATE INDEX IF NOT EXISTS idx_outbox_dispatch ON "
            "outbox_messages(status,available_at,lease_until)",
        ),
        "b57fc621e4a999f0e835034ee620b1dd69f5e41ab6da72e704429da79fd6b2cd",
    ),
    Migration(
        5,
        "versioned-repository-policies",
        (
            "CREATE TABLE IF NOT EXISTS repository_policies (\n"
            "                tenant_id TEXT NOT NULL, repository TEXT NOT NULL, version INTEGER NOT "
            "NULL,\n"
            "                enabled BOOLEAN NOT NULL, auto_fix BOOLEAN NOT NULL,\n"
            "                policy_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL,\n"
            "                PRIMARY KEY(tenant_id,repository))",
            "CREATE TABLE IF NOT EXISTS repository_policy_versions (\n"
            "                id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL,\n"
            "                repository TEXT NOT NULL, version INTEGER NOT NULL,\n"
            "                policy_json JSONB NOT NULL, actor TEXT NOT NULL,\n"
            "                created_at TIMESTAMPTZ NOT NULL,\n"
            "                UNIQUE(tenant_id,repository,version))",
            "CREATE INDEX IF NOT EXISTS idx_repository_policy_versions ON "
            "repository_policy_versions(tenant_id,repository,version DESC)",
        ),
        "5a5c54addedb0b8c07052e0c807888b419d37189b238c84d77a34537ee5cd724",
    ),
    Migration(
        6,
        "model-gateway-usage-ledger",
        (
            "CREATE TABLE IF NOT EXISTS model_usage (\n"
            "                request_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,\n"
            "                repository TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL,\n"
            "                provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,\n"
            "                reserved_tokens BIGINT NOT NULL, input_tokens BIGINT NOT NULL DEFAULT 0,\n"
            "                output_tokens BIGINT NOT NULL DEFAULT 0,\n"
            "                reserved_cost_micros BIGINT NOT NULL DEFAULT 0,\n"
            "                cost_micros BIGINT NOT NULL DEFAULT 0, redactions INTEGER NOT NULL DEFAULT "
            "0,\n"
            "                request_sha256 TEXT NOT NULL, error TEXT, created_at TIMESTAMPTZ NOT NULL,\n"
            "                completed_at TIMESTAMPTZ)",
            "CREATE INDEX IF NOT EXISTS idx_model_usage_budget ON "
            "model_usage(tenant_id,repository,created_at,status)",
            "CREATE INDEX IF NOT EXISTS idx_model_usage_task ON model_usage(task_id,created_at)",
        ),
        "e194bf5592cc86ab548c8ba1eb3cb8f1a13eadf903758ca5b926013b973222fc",
    ),
    Migration(
        7,
        "model-route-attempt-correlation",
        (
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS root_request_id TEXT",
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS route_id TEXT",
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1",
        ),
        "b4e6d5ec114fb3fff0dc8247a14b76b2a6aabd8a9f5e77d4f8f7bdd686c6c831",
    ),
    Migration(
        8,
        "queue-recovery-operations",
        (
            "CREATE INDEX IF NOT EXISTS idx_tasks_recovery ON tasks(cancel_requested,state,created_at,id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_recovery_epoch ON audit_log(action,resource) "
            "WHERE tenant_id='system' AND action='recovery.queue.stage'",
        ),
        "f10d7caedca83ca06f7878412983dfb009377018d4912b519471ebdc46c44c09",
    ),
    Migration(
        9,
        "model-usage-reconciliation",
        (
            "CREATE INDEX IF NOT EXISTS idx_model_usage_reconciliation ON model_usage(status,created_at)",
        ),
        "64de4e9bdbf2f922791da709489cd5ad26de048eb36aacfb3e154d101203427b",
    ),
    Migration(
        10,
        "sanitize-legacy-operational-errors",
        (
            "UPDATE tasks SET error='review execution failed [type=legacy; ref=0000000000000000]' WHERE "
            "error IS NOT NULL AND error<>''",
            "UPDATE trace_events SET message='review execution failed [type=legacy; "
            "ref=0000000000000000]' WHERE state='FAILED'",
            "UPDATE checkpoints SET error='review node failed [type=legacy; ref=0000000000000000]' WHERE "
            "error IS NOT NULL AND error<>''",
            'UPDATE failure_cases SET payload_json=\'{"error":"review execution failed [type=legacy; '
            "ref=0000000000000000]\"}'::jsonb WHERE category='execution_error'",
            'UPDATE agent_messages SET content_json=\'{"error":"review agent failed [type=legacy; '
            "ref=0000000000000000]\"}'::jsonb WHERE kind='agent_failure'",
            "UPDATE outbox_messages SET last_error='outbox dispatch failed [type=legacy; "
            "ref=0000000000000000]' WHERE last_error IS NOT NULL AND last_error<>''",
            "UPDATE effect_receipts SET last_error='external effect failed [type=legacy; "
            "ref=0000000000000000]' WHERE last_error IS NOT NULL AND last_error<>''",
            "UPDATE alerts SET message='task delivery failed [type=legacy; ref=0000000000000000]' WHERE "
            "alert_key LIKE 'dlq:%'",
            'UPDATE audit_log SET detail_json=\'{"error":"shadow review failed [type=legacy; '
            "ref=0000000000000000]\"}'::jsonb WHERE action='shadow.failed'",
        ),
        "754303e396994edad908dda2208874b3e19d967b674f82c303c3ac7ef0400e53",
    ),
    Migration(
        11,
        "model-route-shadow-governance",
        (
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS lane TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE model_usage ADD COLUMN IF NOT EXISTS topology_sha256 TEXT",
            "CREATE TABLE IF NOT EXISTS model_route_shadows (\n"
            "                observation_id TEXT PRIMARY KEY, topology_sha256 TEXT NOT NULL,\n"
            "                root_request_id TEXT NOT NULL, tenant_id TEXT NOT NULL,\n"
            "                repository TEXT NOT NULL, task_id TEXT, purpose TEXT NOT NULL,\n"
            "                active_route_id TEXT NOT NULL, candidate_route_id TEXT NOT NULL,\n"
            "                status TEXT NOT NULL, agreement BOOLEAN,\n"
            "                active_output_sha256 TEXT NOT NULL, candidate_output_sha256 TEXT,\n"
            "                input_sha256 TEXT NOT NULL, input_tokens BIGINT NOT NULL DEFAULT 0,\n"
            "                output_tokens BIGINT NOT NULL DEFAULT 0,\n"
            "                cost_micros BIGINT NOT NULL DEFAULT 0,\n"
            "                duration_ms BIGINT NOT NULL DEFAULT 0, error_type TEXT, error_ref TEXT,\n"
            "                created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ)",
            "CREATE INDEX IF NOT EXISTS idx_model_route_shadows_report ON "
            "model_route_shadows(tenant_id,candidate_route_id,topology_sha256,created_at)",
        ),
        "887f4ae329c3da5a32224f02579fa8be4e25a3eec6faea5e01078c96adddaaef",
    ),
    Migration(
        12,
        "distributed-model-route-capacity",
        (
            "CREATE TABLE IF NOT EXISTS model_route_capacity_leases (\n"
            "                lease_id TEXT PRIMARY KEY, topology_sha256 TEXT NOT NULL,\n"
            "                route_id TEXT NOT NULL, root_request_id TEXT NOT NULL,\n"
            "                expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_model_route_capacity_leases ON "
            "model_route_capacity_leases(route_id,expires_at)",
            "CREATE TABLE IF NOT EXISTS model_route_capacity_windows (\n"
            "                topology_sha256 TEXT NOT NULL, route_id TEXT NOT NULL,\n"
            "                window_start TIMESTAMPTZ NOT NULL, admitted BIGINT NOT NULL DEFAULT 0,\n"
            "                concurrency_rejections BIGINT NOT NULL DEFAULT 0,\n"
            "                rate_rejections BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL,\n"
            "                PRIMARY KEY(topology_sha256,route_id,window_start))",
            "CREATE INDEX IF NOT EXISTS idx_model_route_capacity_windows ON "
            "model_route_capacity_windows(route_id,window_start)",
        ),
        "c3cb204d23203386fbdd2b193aa922668f1fc7b470e819097cddff01d55a23b0",
    ),
    Migration(
        13,
        "operational-history-retention",
        (
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS trace_pruned_at TIMESTAMPTZ",
            "ALTER TABLE session_turns ADD COLUMN IF NOT EXISTS findings_pruned_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS idx_trace_events_retention ON trace_events(created_at,id)",
            "CREATE INDEX IF NOT EXISTS idx_session_findings_retention ON session_findings(created_at,id)",
        ),
        "533baa174381b694dfe1eeedc5ce2e256cefc135f89562f57978d58166b0b043",
    ),
    Migration(
        14,
        "tenant-review-admission",
        (
            "CREATE TABLE IF NOT EXISTS task_admissions (\n"
            "                task_id TEXT PRIMARY KEY REFERENCES tasks(id), tenant_id TEXT NOT NULL,\n"
            "                active BOOLEAN NOT NULL, release_on_failure BOOLEAN NOT NULL,\n"
            "                generation INTEGER NOT NULL,\n"
            "                acquired_at TIMESTAMPTZ NOT NULL, released_at TIMESTAMPTZ,\n"
            "                release_reason TEXT)",
            "CREATE INDEX IF NOT EXISTS idx_task_admissions_tenant_active ON "
            "task_admissions(tenant_id,active,acquired_at)",
            "INSERT INTO "
            "task_admissions(task_id,tenant_id,active,release_on_failure,generation,acquired_at) SELECT "
            "task.id,task.tenant_id,TRUE,NOT EXISTS (SELECT 1 FROM outbox_messages AS outbox WHERE "
            "outbox.topic='review' AND outbox.message_key=task.id),1,task.created_at FROM tasks AS task "
            "WHERE task.state IN ('PENDING','PLANNING','EXECUTING','REVIEWING') ON CONFLICT(task_id) DO "
            "NOTHING",
        ),
        "307b68eef44a942f7a041b09e8c882f7c100833d393da8db75d2dc0a33ee8b78",
    ),
    Migration(
        15,
        "remove-model-routing-control-plane",
        (
            "DROP TABLE IF EXISTS model_route_capacity_leases",
            "DROP TABLE IF EXISTS model_route_capacity_windows",
            "DROP TABLE IF EXISTS model_route_shadows",
            "DROP TABLE IF EXISTS model_usage",
        ),
        "ab544ed9ce4cd4c84b5b60719828ee889cf658589809941fb4101d882ff7acaa",
    ),
    Migration(
        16,
        "single-use-auth-states",
        (
            "CREATE TABLE IF NOT EXISTS consumed_auth_states (\n"
            "                jti TEXT PRIMARY KEY, purpose TEXT NOT NULL,\n"
            "                expires_at BIGINT NOT NULL, consumed_at TIMESTAMPTZ NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_consumed_auth_states_expiry ON "
            "consumed_auth_states(expires_at)",
        ),
        "36bf6b178e82ae131a1365f5f1f9731590817971af35f84e1eb9ce0090543207",
    ),
    Migration(
        17,
        "deployment-invariants",
        (
            "ALTER TABLE deployments\n"
            "                ADD COLUMN generation BIGINT NOT NULL DEFAULT 1,\n"
            "                ADD CONSTRAINT deployments_versions_valid CHECK (\n"
            "                    (stable_version IS NULL OR stable_version > 0) AND\n"
            "                    (candidate_version IS NULL OR candidate_version > 0) AND\n"
            "                    generation > 0),\n"
            "                ADD CONSTRAINT deployments_percentages_valid CHECK (\n"
            "                    canary_percent BETWEEN 0 AND 100 AND\n"
            "                    shadow_percent BETWEEN 0 AND 100),\n"
            "                ADD CONSTRAINT deployments_rates_valid CHECK (\n"
            "                    max_error_rate BETWEEN 0 AND 1 AND\n"
            "                    max_disagreement_rate BETWEEN 0 AND 1),\n"
            "                ADD CONSTRAINT deployments_samples_valid CHECK (\n"
            "                    min_samples > 0 AND samples >= 0 AND errors BETWEEN 0 AND samples AND\n"
            "                    shadow_samples >= 0 AND disagreements BETWEEN 0 AND shadow_samples),\n"
            "                ADD CONSTRAINT deployments_status_valid CHECK (\n"
            "                    status IN ('stable','running','rolled_back','promoted')),\n"
            "                ADD CONSTRAINT deployments_running_candidate_valid CHECK (\n"
            "                    status <> 'running' OR (\n"
            "                        candidate_version IS NOT NULL AND\n"
            "                        candidate_version IS DISTINCT FROM stable_version AND\n"
            "                        (NOT auto_promote OR shadow_percent > 0)))",
        ),
        "9c37baa6e96fde323a55170d1c8fd75ae5e754d75706b7b7739137ba843db4da",
    ),
    Migration(
        18,
        "skill-version-qualification",
        (
            "ALTER TABLE skill_versions\n"
            "                ADD COLUMN qualification TEXT NOT NULL DEFAULT 'rejected'",
            "UPDATE skill_versions SET qualification=CASE\n"
            "                    WHEN active THEN 'legacy' ELSE 'rejected' END",
            "UPDATE skill_versions AS version SET qualification=CASE run.decision\n"
            "                    WHEN 'activated' THEN 'approved'\n"
            "                    WHEN 'approved' THEN 'approved'\n"
            "                    WHEN 'rejected' THEN 'rejected'\n"
            "                    WHEN 'deferred' THEN 'deferred' END\n"
            "                FROM evolution_runs AS run\n"
            "                WHERE run.skill_name=version.skill_name\n"
            "                    AND run.candidate_version=version.version\n"
            "                    AND NOT version.active\n"
            "                    AND run.decision IN ('activated','approved','rejected','deferred')",
            "ALTER TABLE skill_versions\n"
            "                ADD CONSTRAINT skill_versions_qualification_valid CHECK (\n"
            "                    qualification IN ('legacy','approved','rejected','deferred')),\n"
            "                ADD CONSTRAINT skill_versions_active_qualification_valid CHECK (\n"
            "                    NOT active OR qualification='legacy')",
        ),
        "e50e95c344d139ae5ab517b09fe4e05990685dfd837ccbb70d5e61318dd95478",
    ),
    Migration(
        19,
        "credential-version",
        (
            "ALTER TABLE users ADD COLUMN credential_version BIGINT NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD CONSTRAINT users_credential_version_valid "
            "CHECK (credential_version >= 0)",
        ),
        "05c2cb8a94ab36ee51adb0c45542fdc772822f1a05a962b4bda1fdef6d3b0294",
    ),
    Migration(
        20,
        "pull-request-event-order",
        ("ALTER TABLE review_sessions ADD COLUMN last_webhook_at TIMESTAMPTZ",),
        "3de8650af842c21553e40267ec59ebf521a94caef1a954eb201822b3dca9af2e",
    ),
    Migration(
        21,
        "idempotent-shadow-observations",
        (
            "DELETE FROM release_observations AS duplicate USING release_observations AS kept "
            "WHERE duplicate.tenant_id=kept.tenant_id "
            "AND duplicate.skill_name=kept.skill_name "
            "AND duplicate.task_id=kept.task_id AND duplicate.id>kept.id",
            "CREATE UNIQUE INDEX idx_release_observations_task ON "
            "release_observations(tenant_id,skill_name,task_id)",
            "UPDATE deployments SET shadow_samples=0,disagreements=0 WHERE status='running'",
        ),
        "1835d162bbcb1f265d057022fe7499e12cd38fbacd35433c1553f1bb59b2a5cc",
    ),
    Migration(
        22,
        "release-observation-retention",
        (
            "CREATE INDEX idx_release_observations_created_at ON "
            "release_observations(created_at,id)",
        ),
        "c64a17ebbf267c498214cc006a249ab50c94a089138a89ba2ea60f64f5514b02",
    ),
    Migration(
        23,
        "evolution-revision-lookup",
        (
            "CREATE INDEX idx_evolution_runs_skill_candidate_created ON "
            "evolution_runs(skill_name,candidate_version,created_at DESC,id DESC)",
        ),
        "3fcf618219b549bf64b7bd3ae9ac34ddc679a74f4a42417889f81f0665b25468",
    ),
    Migration(
        24,
        "effect-receipt-retention",
        (
            "CREATE INDEX idx_effect_receipts_completed_at ON "
            "effect_receipts(completed_at,effect_key) WHERE status='completed'",
        ),
        "efb22cf02b0e5cadcb1936508d65e3706f8dddfe480ce5adaeb75309757cb923",
    ),
    Migration(
        25,
        "webhook-delivery-retention",
        (
            "CREATE INDEX idx_webhook_deliveries_received_at ON "
            "webhook_deliveries(received_at,delivery_id)",
        ),
        "51408576232f1b9f41e8d9191ebb0771c4e138a45fa0cdcdc21cf624fdcb7f64",
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
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (%s,%s,%s,%s)",
                (migration.version, migration.name, migration.checksum, _now()),
            )
    except SchemaMigrationError:
        raise
    except Exception as exc:
        raise MigrationApplyError(
            "PostgreSQL schema migration failed (%s)" % type(exc).__name__
        ) from exc
    return target_version
