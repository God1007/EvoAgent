"""Direct composition of EvoAgent's built-in components."""

from __future__ import annotations

from dataclasses import dataclass

from .agents import FilteredAgent
from .auth import AuthManager
from .circuit_breaker import CircuitBreaker
from .config import Settings
from .evolution import EvolutionEngine
from .fixer import SafeFixer
from .github import GitHubClient
from .model_gateway import (
    ModelGateway,
    ModelGatewayOptions,
    ModelRoute,
    OpenAICompatibleModelProvider,
    load_model_route,
)
from .observability import AlertManager, Observability
from .policy import RepositoryPolicyResolver
from .ports import ApplicationStorePort, ProofExecutorPort
from .postgres_store import create_store
from .proof import LocalProofExecutor
from .review_engine import ReviewEngine
from .review_extensions import ReviewerContribution
from .reviewer import LocalRuleReviewer
from .rollout import ReleaseManager
from .skills import resolve_container_image
from .verifier import RepairVerifier


@dataclass(frozen=True)
class ApplicationComponents:
    github_breaker: CircuitBreaker
    llm_breaker: CircuitBreaker
    store: ApplicationStorePort
    policies: RepositoryPolicyResolver
    model_gateway: ModelGateway
    proof_executor: ProofExecutorPort
    repair_container_image: str
    observability: Observability
    review_engine: ReviewEngine
    github: GitHubClient
    fixer: SafeFixer
    auth: AuthManager
    releases: ReleaseManager
    alerts: AlertManager
    evolution: EvolutionEngine


def build_components(settings: Settings) -> ApplicationComponents:
    """Build the one application graph the service actually runs."""
    repair_container_image = resolve_container_image(settings.repair_container_image)
    github_breaker = _breaker(settings, "github")
    llm_breaker = _breaker(settings, "llm")
    store = create_store(
        settings.database_url,
        settings.pg_pool_min,
        settings.pg_pool_max,
        settings.pg_pool_timeout,
        settings.pg_statement_timeout_seconds,
        auto_migrate=False,
    )
    model_gateway = None
    observability = None
    try:
        policies = RepositoryPolicyResolver(store)
        model_gateway = _model_gateway(settings, llm_breaker)
        proof_executor = _proof_executor(settings, repair_container_image)
        observability = Observability(settings.otel_service_name, settings.otel_endpoint)
        review_engine = ReviewEngine(
            settings,
            store,
            observability,
            model_gateway,
            (
                ReviewerContribution(
                    "security-review",
                    FilteredAgent("security-agent", LocalRuleReviewer(), ("SEC-",)),
                    description="Security, injection and secret detection",
                    source="evoagent.reviewer.security",
                ),
                ReviewerContribution(
                    "reliability-review",
                    FilteredAgent("reliability-agent", LocalRuleReviewer(), ("REL-",)),
                    description="Reliability and observability review",
                    source="evoagent.reviewer.reliability",
                ),
            ),
        )
        fixer = SafeFixer(
            _repair_verifier(settings, settings.repair_test_command, repair_container_image),
            max_source_bytes=settings.max_diff_bytes * 10,
        )
        auth = AuthManager(
            store,
            settings.auth_secret,
            settings.session_ttl_seconds,
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password,
            settings.default_tenant_id,
            previous_secret=settings.auth_previous_secret,
        )
        releases = ReleaseManager(store, review_engine.execution_revision())
        alerts = AlertManager(store, settings.alert_failure_rate, settings.alert_min_samples)
        evolution = EvolutionEngine(
            store,
            reviewer_factory=(
                review_engine.build_evaluation_reviewer if review_engine.llm_available else None
            ),
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
            timeout_seconds=settings.timeout_seconds,
            execution_revision=review_engine.execution_revision(),
        )
        return ApplicationComponents(
            github_breaker,
            llm_breaker,
            store,
            policies,
            model_gateway,
            proof_executor,
            repair_container_image,
            observability,
            review_engine,
            GitHubClient(
                settings.github_token,
                breaker=github_breaker,
                max_archive_bytes=settings.repair_memory_mb * 1024 * 1024,
            ),
            fixer,
            auth,
            releases,
            alerts,
            evolution,
        )
    except Exception:
        for resource in (observability, model_gateway, store):
            try:
                _close(resource)
            except Exception:
                pass
        raise


def close_components(components: ApplicationComponents) -> None:
    try:
        _close(components.observability)
    finally:
        try:
            _close(components.model_gateway)
        finally:
            _close(components.store)


def _close(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()


def _breaker(settings: Settings, name: str) -> CircuitBreaker:
    return CircuitBreaker(
        name,
        settings.breaker_failure_threshold,
        settings.breaker_reset_seconds,
    )


def _model_gateway(
    settings: Settings,
    primary_breaker: CircuitBreaker,
) -> ModelGateway:
    route: ModelRoute | None
    if settings.llm_routes_file:
        route = load_model_route(settings.llm_routes_file)
    else:
        config = settings.resolved_llm()
        route = (
            ModelRoute(
                provider=str(config["provider"]),
                model=str(config["model"]),
                base_url=str(config["base_url"]),
                api_key=str(config["api_key"]),
                headers=dict(config.get("headers") or {}),
            )
            if config
            else None
        )
    provider = (
        OpenAICompatibleModelProvider(
            settings.llm_allowed_hosts,
            settings.timeout_seconds,
            breaker=primary_breaker,
        )
        if route
        else None
    )
    return ModelGateway(
        route,
        provider,
        ModelGatewayOptions(
            allowed_hosts=settings.llm_allowed_hosts,
            max_input_tokens=settings.llm_max_input_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
        ),
    )


def _proof_executor(settings: Settings, container_image: str | None = None) -> ProofExecutorPort:
    return LocalProofExecutor(lambda command: _repair_verifier(settings, command, container_image))


def _repair_verifier(
    settings: Settings,
    command: str,
    container_image: str | None = None,
) -> RepairVerifier:
    return RepairVerifier(
        command,
        settings.repair_verify_timeout_seconds,
        container_image=(
            settings.repair_container_image if container_image is None else container_image
        ),
        memory_mb=settings.repair_memory_mb,
        pids_limit=settings.repair_pids_limit,
        cpus=settings.repair_cpus,
        require_container=True,
        max_output_bytes=settings.repair_max_output_bytes,
    )
