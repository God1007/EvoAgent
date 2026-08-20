"""Direct composition of EvoAgent's built-in components."""

from __future__ import annotations

from dataclasses import dataclass

from .agents import FilteredAgent
from .auth import AuthManager
from .circuit_breaker import CircuitBreaker
from .config import Settings
from .evolution import EvolutionEngine
from .fix_rules import (
    DebugPrintFixRule,
    HardcodedSecretFixRule,
    SafeYamlLoadFixRule,
    SecureCookieFixRule,
    SubprocessShellFixRule,
)
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
from .verifier import RepairVerifier


@dataclass(frozen=True)
class ApplicationComponents:
    github_breaker: CircuitBreaker
    llm_breaker: CircuitBreaker
    store: ApplicationStorePort
    policies: RepositoryPolicyResolver
    model_gateway: ModelGateway
    proof_executor: ProofExecutorPort
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
    github_breaker = _breaker(settings, "github")
    llm_breaker = _breaker(settings, "llm")
    store = create_store(
        settings.database_url,
        settings.pg_pool_min,
        settings.pg_pool_max,
        settings.pg_pool_timeout,
    )
    model_gateway = None
    try:
        policies = RepositoryPolicyResolver(store)
        model_gateway = _model_gateway(settings, llm_breaker)
        proof_executor = _proof_executor(settings)
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
            RepairVerifier(
                settings.repair_test_command,
                settings.repair_verify_timeout_seconds,
                container_image=settings.repair_container_image,
                memory_mb=settings.repair_memory_mb,
                pids_limit=settings.repair_pids_limit,
                cpus=settings.repair_cpus,
                require_container=settings.repair_require_container,
                max_output_bytes=settings.repair_max_output_bytes,
            ),
            (
                DebugPrintFixRule(),
                SubprocessShellFixRule(),
                HardcodedSecretFixRule(),
                SafeYamlLoadFixRule(),
                SecureCookieFixRule(),
            ),
        )
        auth = AuthManager(
            store,
            settings.auth_secret,
            settings.session_ttl_seconds,
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password,
            settings.default_tenant_id,
        )
        releases = ReleaseManager(store)
        alerts = AlertManager(store, settings.alert_failure_rate, settings.alert_min_samples)
        evolution = EvolutionEngine(
            store,
            reviewer_factory=(
                review_engine.build_llm_reviewer if review_engine.llm_available else None
            ),
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
        )
        return ApplicationComponents(
            github_breaker,
            llm_breaker,
            store,
            policies,
            model_gateway,
            proof_executor,
            observability,
            review_engine,
            GitHubClient(settings.github_token, breaker=github_breaker),
            fixer,
            auth,
            releases,
            alerts,
            evolution,
        )
    except Exception:
        if model_gateway is not None:
            try:
                _close(model_gateway)
            except Exception:
                pass
        try:
            _close(store)
        except Exception:
            pass
        raise


def close_components(components: ApplicationComponents) -> None:
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


def _proof_executor(settings: Settings) -> ProofExecutorPort:
    return LocalProofExecutor(
        lambda command: RepairVerifier(
            command,
            settings.repair_verify_timeout_seconds,
            container_image=settings.repair_container_image,
            memory_mb=settings.repair_memory_mb,
            pids_limit=settings.repair_pids_limit,
            cpus=settings.repair_cpus,
            require_container=True,
            max_output_bytes=settings.repair_max_output_bytes,
        )
    )
