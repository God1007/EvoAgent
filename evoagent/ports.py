"""The few infrastructure boundaries that have more than one real implementation."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .postgres_store import PostgresTaskStore

if TYPE_CHECKING:
    from .model_gateway import ModelRequest, ModelResponse


# PostgreSQL is the application store. These names keep call-site annotations
# descriptive without maintaining dozens of one-implementation Protocols.
ApplicationStorePort = PostgresTaskStore
AgentMessageStorePort = PostgresTaskStore
AuthStorePort = PostgresTaskStore
EvolutionStorePort = PostgresTaskStore
ReleaseStorePort = PostgresTaskStore
AlertStorePort = PostgresTaskStore
RepositoryPolicyStorePort = PostgresTaskStore
SessionApplicationStorePort = PostgresTaskStore
RepairApplicationStorePort = PostgresTaskStore
ReviewApplicationStorePort = PostgresTaskStore
ReviewExecutionStorePort = PostgresTaskStore
ReviewWorkflowStorePort = PostgresTaskStore
WebhookApplicationStorePort = PostgresTaskStore
OutboxStorePort = PostgresTaskStore
RecoveryStorePort = PostgresTaskStore
ServiceStorePort = PostgresTaskStore

# Internal collaborators have one built-in implementation and need no runtime port.
RepairPublisherPort = Any
ReviewReleasePort = Any
ReviewAlertPort = Any
ObservabilityPort = Any


@runtime_checkable
class ModelGatewayPort(Protocol):
    @property
    def configured(self) -> bool: ...

    def route_info(self) -> dict[str, Any]: ...

    def execution_revision(self) -> str: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class ProofExecutorPort(Protocol):
    def execute(self, files: dict[str, str], command: str) -> dict[str, Any]: ...


@runtime_checkable
class TaskQueuePort(Protocol):
    @property
    def backend(self) -> str: ...

    @property
    def durable(self) -> bool: ...

    def submit(self, payload: dict[str, Any], message_id: str = "") -> str: ...

    def dead_letters(self, limit: int = 100) -> list: ...

    def depth(self) -> int: ...

    def oldest_age_seconds(self) -> float: ...

    def dead_letter_depth(self) -> int: ...

    def health(self) -> dict[str, Any]: ...

    def drain(self, timeout_seconds: float = 0.0) -> bool: ...

    def close(self, drain_timeout_seconds: float = 0.0) -> bool: ...


@runtime_checkable
class CodeHostPort(Protocol):
    def fetch_diff(self, url: str, max_bytes: int | None = None) -> str: ...

    def upsert_comment(
        self,
        api_url: str,
        markdown: str,
        marker: str,
        before_write: Callable[[], None] | None = None,
    ) -> None: ...

    def ensure_repository_access(self, repository: str) -> None: ...

    def get_pull_request(self, repository: str, number: int) -> dict: ...

    def get_file(self, repository: str, path: str, ref: str) -> dict: ...

    def get_branch(self, repository: str, branch: str) -> dict | None: ...

    def find_pull_request_by_head(self, repository: str, branch: str, base: str) -> dict | None: ...

    def create_atomic_commit(
        self,
        repository: str,
        branch: str,
        parent_sha: str,
        files: dict[str, str],
        message: str,
        existing_sha: str = "",
        before_write: Callable[[], None] | None = None,
    ) -> dict: ...

    def create_draft_pull_request(
        self,
        repository: str,
        title: str,
        head: str,
        base: str,
        body: str,
        before_write: Callable[[], None] | None = None,
    ) -> dict: ...

    def download_archive(self, repository: str, ref: str) -> bytes: ...
