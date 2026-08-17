"""Default application composition built on the trusted plugin runtime."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from .agents import FilteredAgent
from .auth import AuthManager
from .capabilities import (
    ALERTS,
    AUTH,
    EVOLUTION,
    FIX_RULE,
    FIXER,
    GITHUB_BREAKER,
    GITHUB_CLIENT,
    LLM_BREAKER,
    MODEL_GATEWAY,
    OBSERVABILITY,
    PROOF_EXECUTOR,
    QUEUE_FACTORY,
    RELEASES,
    REPOSITORY_POLICY,
    REVIEW_ENGINE,
    REVIEWER,
    SETTINGS,
    STORE,
)
from .circuit_breaker import CircuitBreaker
from .config import Settings
from .evolution import EvolutionEngine
from .fix_rules import (
    DebugPrintFixRule,
    FixRule,
    HardcodedSecretFixRule,
    SafeYamlLoadFixRule,
    SecureCookieFixRule,
    SubprocessShellFixRule,
)
from .fixer import SafeFixer
from .github import GitHubClient
from .model_gateway import (
    EnterpriseModelGateway,
    ModelGatewayOptions,
    ModelRoute,
    OpenAICompatibleModelProvider,
    load_model_routes,
)
from .observability import AlertManager, Observability
from .plugins import (
    CapabilityKey,
    Plugin,
    PluginContext,
    PluginManifest,
    PluginProfile,
    PluginRuntime,
    ProviderPlugin,
    discover_plugins,
)
from .policy import RepositoryPolicyResolver
from .ports import ApplicationStorePort, AuthStorePort, ProofExecutorPort
from .postgres_store import create_store
from .proof import LocalProofExecutor
from .proof_remote import RemoteProofExecutor
from .review_engine import ReviewEngine
from .review_extensions import ReviewerContribution
from .reviewer import LocalRuleReviewer
from .rollout import ReleaseManager
from .task_queue import TaskQueue
from .verifier import RepairVerifier


class DefaultQueueFactory:
    def __init__(self, settings: Settings):
        self.settings = settings

    def create(
        self,
        handler: Callable[[dict[str, Any]], None],
        on_dead_letter: Callable[[dict[str, Any], str], None],
    ) -> TaskQueue:
        return TaskQueue(
            handler,
            self.settings.async_workers,
            self.settings.redis_url,
            self.settings.queue_max_attempts,
            self.settings.queue_lease_seconds,
            on_dead_letter,
        )


def build_application_runtime(
    settings: Settings,
    plugins: Sequence[Plugin] = (),
    profile: PluginProfile | None = None,
) -> PluginRuntime:
    """Build and start one candidate application graph transactionally.

    Extra plugins replace a built-in plugin with the same stable plugin id.
    Installed entry-point plugins are loaded only when discovery is explicitly
    enabled and their ids are present in the operator allowlist.
    """

    if profile is None:
        profile_paths = [
            path for path in (settings.plugin_profile_path, *settings.plugin_profile_layers) if path
        ]
        if profile_paths:
            profile = PluginProfile.from_toml_layers(profile_paths)
    selected_profile = profile or PluginProfile()
    catalog = {plugin.manifest.plugin_id: plugin for plugin in default_plugins(settings)}
    discovered: list[Plugin] = []
    if settings.plugin_discovery:
        if not settings.plugin_allowlist:
            raise ValueError(
                "EVOAGENT_PLUGIN_ALLOWLIST is required when trusted plugin discovery is enabled"
            )
        discovered = discover_plugins(settings.plugin_allowlist)
    for plugin in [*discovered, *plugins]:
        catalog[plugin.manifest.plugin_id] = plugin
    return PluginRuntime(list(catalog.values()), selected_profile).start()


def default_plugins(settings: Settings) -> list[Plugin]:
    """Return the replaceable built-in provider catalog."""

    return [
        _provider(
            "evoagent.settings",
            SETTINGS,
            (),
            lambda _context: settings,
            "Root immutable application settings",
        ),
        _provider(
            "evoagent.breaker.github",
            GITHUB_BREAKER,
            (SETTINGS,),
            lambda context: _breaker(context, "github"),
            "GitHub dependency circuit breaker",
        ),
        _provider(
            "evoagent.breaker.llm",
            LLM_BREAKER,
            (SETTINGS,),
            lambda context: _breaker(context, "llm"),
            "LLM dependency circuit breaker",
        ),
        _provider(
            "evoagent.store",
            STORE,
            (SETTINGS,),
            _store,
            "SQLite/PostgreSQL task and audit store",
            close=_close_resource,
        ),
        _provider(
            "evoagent.policy.repository",
            REPOSITORY_POLICY,
            (STORE,),
            lambda context: RepositoryPolicyResolver(context.require(STORE)),
            "Versioned tenant and repository execution policy",
        ),
        _provider(
            "evoagent.model-gateway",
            MODEL_GATEWAY,
            (SETTINGS, STORE, LLM_BREAKER),
            _model_gateway,
            "Governed model routing, redaction, budget, and usage ledger",
        ),
        _provider(
            "evoagent.proof-executor",
            PROOF_EXECUTOR,
            (SETTINGS,),
            _proof_executor,
            "Container-isolated local or authenticated remote proof execution",
        ),
        _provider(
            "evoagent.observability",
            OBSERVABILITY,
            (SETTINGS,),
            lambda context: _observability(context),
            "OpenTelemetry tracing provider",
        ),
        _provider(
            "evoagent.codehost.github",
            GITHUB_CLIENT,
            (SETTINGS, GITHUB_BREAKER),
            lambda context: GitHubClient(
                context.require(SETTINGS).github_token,
                breaker=context.require(GITHUB_BREAKER),
            ),
            "GitHub code-host adapter",
        ),
        _provider(
            "evoagent.reviewer.security",
            REVIEWER,
            (),
            lambda _context: ReviewerContribution(
                "security-review",
                FilteredAgent("security-agent", LocalRuleReviewer(), ("SEC-",)),
                description="Security, injection and secret detection",
                source="evoagent.reviewer.security",
            ),
            "Built-in deterministic security reviewer contribution",
            priority=20,
        ),
        _provider(
            "evoagent.reviewer.reliability",
            REVIEWER,
            (),
            lambda _context: ReviewerContribution(
                "reliability-review",
                FilteredAgent("reliability-agent", LocalRuleReviewer(), ("REL-",)),
                description="Reliability and observability review",
                source="evoagent.reviewer.reliability",
            ),
            "Built-in deterministic reliability reviewer contribution",
            priority=10,
        ),
        _provider(
            "evoagent.review-engine",
            REVIEW_ENGINE,
            (SETTINGS, STORE, OBSERVABILITY, MODEL_GATEWAY, REVIEWER),
            lambda context: ReviewEngine(
                context.require(SETTINGS),
                context.require(STORE),
                context.require(OBSERVABILITY),
                context.require(MODEL_GATEWAY),
                context.all(REVIEWER),
            ),
            "Default multi-agent review workflow",
        ),
        _provider(
            "evoagent.fix-rule.debug-print",
            FIX_RULE,
            (),
            lambda _context: DebugPrintFixRule(),
            "Remove deterministic Python debug print statements",
        ),
        _provider(
            "evoagent.fix-rule.subprocess-shell",
            FIX_RULE,
            (),
            lambda _context: SubprocessShellFixRule(),
            "Disable literal shell=True subprocess execution",
        ),
        _provider(
            "evoagent.fix-rule.hardcoded-secret",
            FIX_RULE,
            (),
            lambda _context: HardcodedSecretFixRule(),
            "Move literal Python secrets to environment variables",
        ),
        _provider(
            "evoagent.fix-rule.safe-yaml-load",
            FIX_RULE,
            (),
            lambda _context: SafeYamlLoadFixRule(),
            "Replace unambiguous yaml.load calls with yaml.safe_load",
        ),
        _provider(
            "evoagent.fix-rule.secure-cookie",
            FIX_RULE,
            (),
            lambda _context: SecureCookieFixRule(),
            "Enable explicit insecure cookie Secure flags",
        ),
        _provider(
            "evoagent.fixer",
            FIXER,
            (SETTINGS, FIX_RULE),
            lambda context: _fixer(
                context.require(SETTINGS),
                context.all(FIX_RULE),
            ),
            "Verified deterministic repair provider",
        ),
        _provider(
            "evoagent.auth",
            AUTH,
            (SETTINGS, STORE),
            lambda context: _auth(context.require(SETTINGS), context.require(STORE)),
            "JWT, RBAC, and tenant authorization",
        ),
        _provider(
            "evoagent.releases",
            RELEASES,
            (STORE,),
            lambda context: ReleaseManager(context.require(STORE)),
            "Canary and shadow release governance",
        ),
        _provider(
            "evoagent.alerts",
            ALERTS,
            (SETTINGS, STORE),
            lambda context: AlertManager(
                context.require(STORE),
                context.require(SETTINGS).alert_failure_rate,
                context.require(SETTINGS).alert_min_samples,
            ),
            "Failure-rate alert evaluation",
        ),
        _provider(
            "evoagent.evolution",
            EVOLUTION,
            (SETTINGS, STORE, REVIEW_ENGINE),
            lambda context: _evolution(context),
            "Prompt evaluation and evolution governance",
        ),
        _provider(
            "evoagent.queue-factory",
            QUEUE_FACTORY,
            (SETTINGS,),
            lambda context: DefaultQueueFactory(context.require(SETTINGS)),
            "Memory/Redis Streams queue factory",
        ),
    ]


def _provider(
    plugin_id: str,
    capability: CapabilityKey[Any],
    requires: tuple[CapabilityKey[Any], ...],
    factory: Callable[[PluginContext], Any],
    description: str,
    close: Callable[[Any], None] | None = None,
    priority: int = 0,
) -> ProviderPlugin[Any]:
    return ProviderPlugin(
        PluginManifest(
            plugin_id=plugin_id,
            version="1.0.0",
            provides=(capability.name,),
            requires=tuple(item.name for item in requires),
            priority=priority,
            description=description,
        ),
        capability,
        factory,
        close=close,
    )


def _breaker(context: PluginContext, name: str) -> CircuitBreaker:
    settings = context.require(SETTINGS)
    return CircuitBreaker(
        name,
        settings.breaker_failure_threshold,
        settings.breaker_reset_seconds,
    )


def _store(context: PluginContext) -> ApplicationStorePort:
    settings = context.require(SETTINGS)
    return create_store(
        settings.database_url,
        settings.db_path,
        settings.pg_pool_min,
        settings.pg_pool_max,
        settings.pg_pool_timeout,
    )


def _observability(context: PluginContext) -> Observability:
    settings = context.require(SETTINGS)
    return Observability(settings.otel_service_name, settings.otel_endpoint)


def _model_gateway(context: PluginContext) -> EnterpriseModelGateway:
    settings = context.require(SETTINGS)
    if settings.llm_routes_file:
        routes = load_model_routes(settings.llm_routes_file)
    else:
        config = settings.resolved_llm()
        routes = (
            (
                ModelRoute(
                    provider=str(config["provider"]),
                    model=str(config["model"]),
                    base_url=str(config["base_url"]),
                    api_key=str(config["api_key"]),
                    headers=dict(config.get("headers") or {}),
                    input_cost_micros_per_million=settings.llm_input_cost_micros_per_million,
                    output_cost_micros_per_million=settings.llm_output_cost_micros_per_million,
                    route_id="default",
                ),
            )
            if config
            else ()
        )
    providers = {}
    primary_breaker = context.require(LLM_BREAKER)
    for index, route in enumerate(routes):
        breaker = (
            primary_breaker
            if index == 0
            else CircuitBreaker(
                "llm:%s" % (route.route_id or index),
                settings.breaker_failure_threshold,
                settings.breaker_reset_seconds,
            )
        )
        providers[route.route_id or "%s-%s" % (route.provider, route.model)] = (
            OpenAICompatibleModelProvider(
                settings.llm_allowed_hosts,
                settings.timeout_seconds,
                breaker=breaker,
            )
        )
    return EnterpriseModelGateway(
        context.require(STORE),
        routes,
        providers,
        ModelGatewayOptions(
            allowed_hosts=settings.llm_allowed_hosts,
            max_input_tokens=settings.llm_max_input_tokens,
            max_output_tokens=settings.llm_max_output_tokens,
            daily_token_budget=settings.llm_daily_token_budget,
            daily_cost_micros=settings.llm_daily_cost_micros,
            fallback_attempts=settings.llm_fallback_attempts,
            reservation_ttl_seconds=settings.llm_reservation_ttl_seconds,
        ),
    )


def _fixer(settings: Settings, rules: list[FixRule]) -> SafeFixer:
    return SafeFixer(
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
        rules,
    )


def _proof_executor(context: PluginContext) -> ProofExecutorPort:
    settings = context.require(SETTINGS)
    if settings.proof_runner_url:
        return RemoteProofExecutor(
            settings.proof_runner_url,
            settings.proof_runner_signing_key,
            settings.proof_runner_allowed_hosts,
            timeout_seconds=settings.proof_runner_timeout_seconds,
            max_request_bytes=settings.proof_runner_max_request_bytes,
            max_response_bytes=settings.proof_runner_max_response_bytes,
            replay_window_seconds=settings.proof_runner_replay_window_seconds,
            signing_key_id=settings.proof_runner_signing_key_id,
        )
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


def _auth(settings: Settings, store: AuthStorePort) -> AuthManager:
    return AuthManager(
        store,
        settings.auth_secret,
        settings.session_ttl_seconds,
        settings.bootstrap_admin_username,
        settings.bootstrap_admin_password,
        settings.default_tenant_id,
    )


def _evolution(context: PluginContext) -> EvolutionEngine:
    settings = context.require(SETTINGS)
    engine = context.require(REVIEW_ENGINE)
    return EvolutionEngine(
        context.require(STORE),
        reviewer_factory=engine.build_llm_reviewer if engine.llm_available else None,
        min_cases=settings.eval_min_cases,
        max_cases=settings.eval_max_cases,
        min_improvement=settings.eval_min_improvement,
        min_holdout_cases=settings.eval_min_holdout_cases,
        max_metric_regression=settings.eval_max_metric_regression,
    )


def _close_resource(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        close()
