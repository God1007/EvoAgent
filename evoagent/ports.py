"""Domain ports implemented by EvoAgent infrastructure adapters.

The application depends on these behavioral contracts instead of SQLite,
PostgreSQL, Redis, or GitHub implementation classes.  Protocols keep the
boundary structural: an enterprise plugin may provide an adapter without
inheriting from an EvoAgent base class, while static checks and contract tests
still verify the surface consumed by the domain.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from .models import ReviewReport, TraceEvent


class ReviewExecutionStorePort(Protocol):
    def create(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        payload: dict[str, Any],
        tenant_id: str = "default",
    ) -> None: ...

    def transition(self, task_id: str, event: TraceEvent) -> None: ...

    def succeed(self, task_id: str, report: ReviewReport, event: TraceEvent) -> None: ...

    def fail(self, task_id: str, error: str, event: TraceEvent) -> None: ...

    def cancel(self, task_id: str, event: TraceEvent) -> None: ...

    def get(self, task_id: str, tenant_id: str | None = None) -> dict[str, Any] | None: ...

    def save_checkpoint(
        self,
        task_id: str,
        node: str,
        state: dict[str, Any],
        status: str = "completed",
        attempt: int = 1,
        error: str = "",
    ) -> None: ...

    def load_checkpoints(self, task_id: str) -> dict[str, dict[str, Any]]: ...

    def record_failure_case(self, task_id: str, category: str, payload: dict[str, Any]) -> None: ...

    def is_cancelled(self, task_id: str) -> bool: ...


class AgentMessageStorePort(Protocol):
    def record_agent_message(self, task_id: str, message: dict[str, Any]) -> None: ...


class SkillVersionReadStorePort(Protocol):
    def get_active_skill_version(self, skill_name: str) -> dict[str, Any] | None: ...


class ReviewWorkflowStorePort(
    ReviewExecutionStorePort,
    AgentMessageStorePort,
    SkillVersionReadStorePort,
    Protocol,
):
    """Persistence required while assembling and executing a review graph."""


class AuthStorePort(Protocol):
    def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        tenant_id: str,
        role: str,
    ) -> None: ...

    def get_user(self, username: str) -> dict[str, Any] | None: ...


class EvolutionStorePort(SkillVersionReadStorePort, Protocol):
    def save_evaluation_case(
        self,
        name: str,
        split: str,
        diff: str,
        expected: list,
        source: str = "manual",
        active: bool = True,
    ) -> dict[str, Any]: ...

    def list_evaluation_cases(
        self,
        split: str | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list: ...

    def save_evolution_run(self, run: dict[str, Any]) -> dict[str, Any]: ...

    def list_evolution_runs(self, limit: int = 50) -> list: ...

    def save_skill_version(
        self,
        skill_name: str,
        prompt: str,
        score: float,
        activate: bool = False,
    ) -> dict[str, Any]: ...

    def list_skill_versions(self, skill_name: str) -> list: ...

    def activate_skill_version(self, skill_name: str, version: int) -> bool: ...

    def list_failure_cases(
        self,
        unresolved_only: bool = False,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list: ...

    def resolve_failure_cases(self, case_ids: list) -> None: ...


class ReleaseStorePort(Protocol):
    def save_deployment(self, tenant_id: str, skill_name: str, config: dict[str, Any]) -> None: ...

    def get_deployment(self, tenant_id: str, skill_name: str) -> dict[str, Any] | None: ...

    def record_deployment_result(
        self, tenant_id: str, skill_name: str, failed: bool
    ) -> dict[str, Any] | None: ...

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
    ) -> dict[str, Any] | None: ...

    def create_alert(self, tenant_id: str, alert_key: str, severity: str, message: str) -> None: ...


class AlertStorePort(Protocol):
    def dashboard_stats(self, tenant_id: str | None = None) -> dict[str, Any]: ...

    def create_alert(self, tenant_id: str, alert_key: str, severity: str, message: str) -> None: ...


class OutboxStorePort(Protocol):
    def claim_outbox(
        self,
        owner: str,
        limit: int,
        lease_seconds: float,
        max_attempts: int,
    ) -> list[dict[str, Any]]: ...

    def mark_outbox_published(self, message_id: str, owner: str) -> bool: ...

    def release_outbox(
        self,
        message_id: str,
        owner: str,
        error: str,
        retry_delay_seconds: float,
        max_attempts: int,
    ) -> bool: ...

    def outbox_stats(self) -> dict[str, Any]: ...

    def list_outbox(self, status: str = "dead", limit: int = 100) -> list: ...

    def requeue_outbox(self, message_id: str) -> bool: ...


class ServiceStorePort(Protocol):
    def ping(self) -> None: ...

    def schema_version(self) -> int: ...

    def get(self, task_id: str, tenant_id: str | None = None) -> dict[str, Any] | None: ...

    def create_review_task(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        payload: dict[str, Any],
        tenant_id: str,
        diff: str | None = None,
        outbox_payload: dict[str, Any] | None = None,
    ) -> None: ...

    def list_tasks(self, limit: int = 50, tenant_id: str | None = None) -> list: ...

    def save_task_payload(self, task_id: str, diff: str) -> None: ...

    def get_task_payload(self, task_id: str) -> str | None: ...

    def update_task_input(self, task_id: str, updates: dict[str, Any]) -> None: ...

    def request_cancel(self, task_id: str, tenant_id: str | None = None) -> bool: ...

    def start_session_turn(
        self,
        tenant_id: str,
        repository: str,
        pull_request: int,
        head_sha: str | None,
        trigger: str,
        task_id: str | None = None,
    ) -> dict[str, Any]: ...

    def complete_session_turn(
        self,
        session_id: str,
        turn_id: str,
        task_id: str | None,
        open_snapshots: list[dict[str, Any]],
        summary: dict[str, Any],
        head_sha: str | None = None,
    ) -> None: ...

    def previous_open_snapshot(self, session_id: str, turn_id: str) -> list[dict[str, Any]]: ...

    def get_session(
        self, tenant_id: str, repository: str, pull_request: int
    ) -> dict[str, Any] | None: ...

    def get_session_timeline(
        self, session_id: str, tenant_id: str | None = None, turn_limit: int = 200
    ) -> dict[str, Any] | None: ...

    def resolve_session_input(self, session_id: str) -> None: ...

    def claim_webhook(
        self,
        delivery_id: str,
        tenant_id: str,
        event_type: str,
        payload_sha256: str,
    ) -> bool: ...

    def complete_webhook(self, delivery_id: str, task_id: str | None) -> None: ...

    def get_webhook(self, delivery_id: str) -> dict[str, Any] | None: ...

    def repository_allowed(
        self, tenant_id: str, repository: str, require_auto_fix: bool = False
    ) -> bool: ...

    def audit(
        self,
        tenant_id: str,
        actor: str,
        action: str,
        resource: str,
        detail: dict[str, Any] | None = None,
    ) -> None: ...

    def list_audit(self, tenant_id: str, limit: int = 100) -> list: ...

    def create_alert(self, tenant_id: str, alert_key: str, severity: str, message: str) -> None: ...

    def list_alerts(self, tenant_id: str, limit: int = 100) -> list: ...

    def save_installation(
        self, installation_id: int, account_login: str, tenant_id: str = "default"
    ) -> None: ...

    def installation_tenant(self, installation_id: int) -> str | None: ...

    def record_failure_case(self, task_id: str, category: str, payload: dict[str, Any]) -> None: ...

    def list_failure_cases(
        self,
        unresolved_only: bool = False,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> list: ...

    def claim_effect(self, effect_key: str, owner: str, lease_seconds: float) -> dict[str, Any]: ...

    def complete_effect(self, effect_key: str, owner: str, result: dict[str, Any]) -> bool: ...

    def release_effect(self, effect_key: str, owner: str, error: str) -> bool: ...


@runtime_checkable
class ApplicationStorePort(
    ReviewWorkflowStorePort,
    AuthStorePort,
    EvolutionStorePort,
    ReleaseStorePort,
    AlertStorePort,
    OutboxStorePort,
    ServiceStorePort,
    Protocol,
):
    """Complete persistence capability supplied to the application runtime."""


@runtime_checkable
class TaskQueuePort(Protocol):
    @property
    def backend(self) -> str: ...

    @property
    def durable(self) -> bool: ...

    def submit(self, payload: dict[str, Any], message_id: str = "") -> str: ...

    def dead_letters(self, limit: int = 100) -> list: ...

    def replay_dead_letter(self, message_id: str) -> bool: ...

    def depth(self) -> int: ...

    def drain(self, timeout_seconds: float = 0.0) -> bool: ...

    def close(self, drain_timeout_seconds: float = 0.0) -> bool: ...


class QueueFactoryPort(Protocol):
    def create(
        self,
        handler: Callable[[dict[str, Any]], None],
        on_dead_letter: Callable[[dict[str, Any], str], None],
    ) -> TaskQueuePort: ...


@runtime_checkable
class CodeHostPort(Protocol):
    """Code-host operations used by review delivery and deterministic repair."""

    def fetch_diff(self, url: str, max_bytes: int | None = None) -> str: ...

    def upsert_comment(self, api_url: str, markdown: str, marker: str) -> None: ...

    def ensure_repository_access(self, repository: str) -> None: ...

    def get_pull_request(self, repository: str, number: int) -> dict: ...

    def get_file(self, repository: str, path: str, ref: str) -> dict: ...

    def get_branch(self, repository: str, branch: str) -> dict | None: ...

    def find_pull_request_by_head(self, repository: str, branch: str) -> dict | None: ...

    def create_atomic_commit(
        self,
        repository: str,
        branch: str,
        parent_sha: str,
        files: dict[str, str],
        message: str,
    ) -> dict: ...

    def create_draft_pull_request(
        self,
        repository: str,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> dict: ...

    def download_archive(self, repository: str, ref: str) -> bytes: ...
