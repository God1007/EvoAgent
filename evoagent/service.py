import threading
import time
from collections.abc import Sequence
from typing import Any

from .application import (
    ModelUsageUseCases,
    PolicyUseCases,
    RepairOptions,
    RepairUseCases,
    ReviewOptions,
    ReviewUseCases,
    SessionUseCases,
    WebhookOptions,
    WebhookUseCases,
)
from .backpressure import ConcurrencyLimiter, RateLimiter, TrustedProxyResolver
from .bootstrap import build_application_runtime
from .capabilities import (
    ALERTS,
    AUTH,
    EVOLUTION,
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
    STORE,
)
from .config import Settings
from .diff_parser import parse_unified_diff
from .errors import coerce_safe_summary, safe_exception_summary
from .github import GitHubAppAuthenticator, GitHubClient
from .metrics import metrics
from .outbox import OutboxDispatcher
from .plugins import Plugin, PluginProfile, PluginRuntime
from .ports import CodeHostPort
from .retention import RetentionManager, RetentionOptions
from .review_engine import ReviewEngine
from .reviewer import GatewayReviewer


class ReviewService:
    def __init__(
        self,
        settings: Settings,
        *,
        plugins: Sequence[Plugin] = (),
        plugin_profile: PluginProfile | None = None,
    ):
        self.settings = settings
        settings.validate_evolution()
        self.plugin_runtime: PluginRuntime = build_application_runtime(
            settings,
            plugins,
            plugin_profile,
        )
        self._closed = False
        try:
            self.github_breaker = self.plugin_runtime.require(GITHUB_BREAKER)
            self.llm_breaker = self.plugin_runtime.require(LLM_BREAKER)
            self.model_gateway = self.plugin_runtime.require(MODEL_GATEWAY)
            self.store = self.plugin_runtime.require(STORE)
            self.policies = self.plugin_runtime.require(REPOSITORY_POLICY)
            self.observability = self.plugin_runtime.require(OBSERVABILITY)
            self.review_engine: ReviewEngine = self.plugin_runtime.require(REVIEW_ENGINE)
            self.llm_config = self.review_engine.llm_config
            self.registry = self.review_engine.registry
            self.reviewer = self.review_engine.reviewer
            self.harness = self.review_engine.harness
            self.github = self.plugin_runtime.require(GITHUB_CLIENT)
            self.fixer = self.plugin_runtime.require(FIXER)
            self.proof_executor = self.plugin_runtime.require(PROOF_EXECUTOR)
            self.auth = self.plugin_runtime.require(AUTH)
            self.releases = self.plugin_runtime.require(RELEASES)
            self.alerts = self.plugin_runtime.require(ALERTS)
            self.evolution = self.plugin_runtime.require(EVOLUTION)
            self.session_use_cases = SessionUseCases(self.store, settings.max_diff_bytes)
            self.policy_use_cases = PolicyUseCases(
                self.store,
                self.policies,
                lambda: tuple(getattr(self.fixer, "rule_ids", ())),
            )
            self.model_usage_use_cases = ModelUsageUseCases(self.store)
            self.repair_use_cases = RepairUseCases(
                self.store,
                self.policies,
                self.fixer,
                lambda installation_id: self.github_client_for_installation(installation_id),
                self._publish_event,
                RepairOptions(
                    max_diff_bytes=settings.max_diff_bytes,
                    verify_timeout_seconds=settings.repair_verify_timeout_seconds,
                    container_image=settings.repair_container_image,
                    memory_mb=settings.repair_memory_mb,
                    pids_limit=settings.repair_pids_limit,
                    cpus=settings.repair_cpus,
                    max_output_bytes=settings.repair_max_output_bytes,
                    effect_lease_seconds=settings.effect_lease_seconds,
                ),
                self.proof_executor,
            )
            self.review_use_cases = ReviewUseCases(
                self.store,
                self.policies,
                self.releases,
                self.alerts,
                self.observability,
                lambda: self.queue,
                lambda: self.outbox.notify(),
                lambda: (
                    self.reviewer.name,
                    str(self.llm_config.get("provider", "local")),
                    str(self.llm_config.get("model", "")),
                ),
                lambda task_id, repository, pull_request, diff, tenant_id: self._run_review(
                    task_id, repository, pull_request, diff, tenant_id
                ),
                lambda task_id, tenant_id, diff, report: self._run_shadow(
                    task_id, tenant_id, diff, report
                ),
                lambda payload, report: self._record_session_turn(payload, report),
                lambda installation_id: self.github_client_for_installation(installation_id),
                self._publish_event,
                ReviewOptions(
                    max_diff_bytes=settings.max_diff_bytes,
                    queue_lease_seconds=settings.queue_lease_seconds,
                    auto_post_review=settings.auto_post_review,
                    tenant_max_active_reviews=settings.tenant_max_active_reviews,
                ),
                lambda tenant_id, repository: (
                    tuple(self.model_gateway.route_catalog(tenant_id, repository))
                    if self.model_gateway.configured
                    else None
                ),
            )
            self.webhook_use_cases = WebhookUseCases(
                self.store,
                self.review_use_cases,
                lambda: self.queue,
                lambda: self.outbox.notify(),
                WebhookOptions(
                    default_tenant_id=settings.default_tenant_id,
                    auto_post_review=settings.auto_post_review,
                ),
            )
            self.queue = self.plugin_runtime.require(QUEUE_FACTORY).create(
                self._process_queued,
                self._on_dead_letter,
            )
            self.outbox = OutboxDispatcher(
                self.store,
                self.queue,
                settings.outbox_poll_seconds,
                settings.outbox_batch_size,
                settings.outbox_lease_seconds,
                settings.outbox_max_attempts,
            )
            self.retention = RetentionManager(
                self.store,
                RetentionOptions(
                    retention_days=settings.history_retention_days,
                    interval_seconds=settings.history_maintenance_seconds,
                    batch_size=settings.history_prune_batch_size,
                ),
            )
        except Exception:
            retention = getattr(self, "retention", None)
            if retention is not None:
                retention.close()
            outbox = getattr(self, "outbox", None)
            if outbox is not None:
                outbox.close()
            queue = getattr(self, "queue", None)
            if queue is not None:
                queue.close()
            try:
                self.plugin_runtime.stop()
            except Exception:
                metrics.inc("plugin_cleanup_failures_total")
            raise
        metrics.register_gauge_source("queue_depth", self.queue.depth)
        metrics.register_gauge_source("queue_oldest_age_seconds", self.queue.oldest_age_seconds)
        metrics.register_gauge_source("dead_letter_depth", self.queue.dead_letter_depth)
        metrics.register_gauge_source(
            "outbox_pending", lambda: float(self.store.outbox_stats()["pending"])
        )
        metrics.register_gauge_source(
            "outbox_dead", lambda: float(self.store.outbox_stats()["dead"])
        )
        metrics.register_gauge_source(
            "outbox_oldest_age_seconds",
            lambda: float(self.store.outbox_stats()["oldest_age_seconds"]),
        )
        # Admission control: per-client rate limit + a bounded gate for the
        # CPU/sandbox-heavy endpoints so overload sheds instead of collapsing.
        self.rate_limiter = RateLimiter(
            settings.rate_limit_rps,
            settings.rate_limit_burst or settings.rate_limit_rps,
        )
        self.client_identity_resolver = TrustedProxyResolver(settings.trusted_proxy_cidrs)
        self.heavy_gate = ConcurrencyLimiter(settings.max_inflight_heavy)
        metrics.register_gauge_source("heavy_in_flight", self.heavy_gate.in_flight)
        metrics.register_gauge_source("breaker_github_state", self.github_breaker.state_code)
        metrics.register_gauge_source(
            "breaker_llm_state",
            getattr(self.model_gateway, "breaker_state_code", self.llm_breaker.state_code),
        )
        metrics.register_gauge_source(
            "plugins_loaded",
            lambda: float(len(self.plugin_runtime.describe()["plugins"])),
        )
        metrics.register_gauge_source(
            "plugin_runtime_ready",
            lambda: 1.0 if self.plugin_runtime.state.value == "running" else 0.0,
        )
        metrics.register_gauge_source(
            "retention_enabled", lambda: 1.0 if self.retention.enabled else 0.0
        )
        metrics.register_gauge_source(
            "retention_last_success_timestamp_seconds",
            self.retention.last_success_timestamp,
        )
        metrics.register_gauge_source(
            "retention_maintenance_interval_seconds",
            lambda: float(self.settings.history_maintenance_seconds),
        )
        metrics.register_gauge_source(
            "review_admission_capacity_enabled",
            lambda: 1.0 if self.settings.tenant_max_active_reviews > 0 else 0.0,
        )
        metrics.register_gauge_source(
            "review_admission_limit",
            lambda: float(self.settings.tenant_max_active_reviews),
        )
        metrics.register_gauge_source(
            "review_admission_slots_active",
            lambda: float(self.store.tenant_review_admission_stats()["active"]),
        )
        self._readiness_lock = threading.Lock()
        self._readiness_cache: tuple[float, tuple[bool, dict[str, Any]]] | None = None
        self._readiness_ttl = 1.0
        self._register_pool_metrics()
        self._publish_event(
            "service.started",
            {
                "queue_backend": self.queue.backend,
                "llm_provider": self.llm_config.get("provider", "local"),
            },
        )

    def close(self) -> None:
        """Release owned resources in reverse dependency order."""
        if self._closed:
            return
        self._closed = True
        self._publish_event("service.stopping", {"queue_backend": self.queue.backend})
        try:
            if not self.retention.close(self.settings.queue_shutdown_timeout_seconds):
                metrics.inc("retention_shutdown_timeouts_total")
            if not self.outbox.close(self.settings.queue_shutdown_timeout_seconds):
                metrics.inc("outbox_shutdown_timeouts_total")
            drained = self.queue.close(self.settings.queue_shutdown_timeout_seconds)
            if not drained:
                metrics.inc("queue_drain_timeouts_total")
                self._publish_event(
                    "queue.drain-timeout",
                    {
                        "queue_backend": self.queue.backend,
                        "timeout_seconds": self.settings.queue_shutdown_timeout_seconds,
                    },
                )
        finally:
            self.plugin_runtime.stop()

    def _publish_event(self, name: str, payload: dict[str, Any]) -> None:
        runtime = getattr(self, "plugin_runtime", None)
        if runtime is None:
            return
        failures = runtime.publish(name, payload)
        if failures:
            metrics.inc("plugin_event_failures_total", len(failures))

    def plugin_status(self) -> dict[str, Any]:
        """Safe runtime inventory for health/debug endpoints."""
        return self.plugin_runtime.describe()

    def retention_status(self) -> dict[str, Any]:
        return self.retention.status()

    def review_admission_status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.tenant_max_active_reviews > 0,
            "max_active_reviews": self.settings.tenant_max_active_reviews,
            "retry_seconds": self.settings.tenant_capacity_retry_seconds,
        }

    def replay_outbox(self, message_id: str) -> bool:
        replayed = self.store.requeue_outbox(message_id)
        if replayed:
            self.outbox.notify()
        return replayed

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
    ) -> dict[str, Any]:
        return self.model_usage_use_cases.reconcile(
            tenant_id,
            actor,
            request_id,
            status,
            input_tokens,
            output_tokens,
            cost_micros,
            error,
        )

    def model_route_promotion_report(
        self, tenant_id: str, candidate_route_id: str, repository: str | None = None
    ) -> dict[str, Any]:
        return self.model_gateway.promotion_report(tenant_id, candidate_route_id, repository)

    def model_route_capacity_report(
        self, tenant_id: str, repository: str | None = None
    ) -> dict[str, Any]:
        return self.model_gateway.capacity_report(tenant_id, repository)

    def tenant_review_capacity_report(self, tenant_id: str) -> dict[str, Any]:
        return self.review_use_cases.tenant_capacity_report(tenant_id)

    def _register_pool_metrics(self) -> None:
        """Expose Postgres pool utilization. Registered in both branches (with a
        constant 0 when unpooled) so the gauge set is consistent. Gated on the
        pool's existence, not a probe call, so a transient stats hiccup can't
        permanently disable the gauges. Stat key names vary across psycopg_pool
        versions, so probe defensively."""
        has_pool = getattr(self.store, "has_pool", None)
        stats = getattr(self.store, "pool_stats", None)
        pooled = bool(has_pool and has_pool())

        def _stat(*keys: str):
            def _read() -> float:
                if not pooled or stats is None:
                    return 0.0
                current = stats() or {}
                for key in keys:
                    if key in current:
                        return float(current[key])
                return 0.0

            return _read

        metrics.register_gauge_source("pg_pool_size", _stat("pool_size"))
        metrics.register_gauge_source("pg_pool_available", _stat("pool_available"))
        metrics.register_gauge_source(
            "pg_pool_waiting", _stat("requests_waiting", "requests_queued")
        )

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """Dependency readiness for orchestration (distinct from liveness). Ready
        means the store is reachable and the queue is observable.

        The result is cached for a short TTL so an unauthenticated ``/ready``
        flood cannot amplify into one fresh DB connection (and Redis round-trip)
        per request."""
        now = time.monotonic()
        with self._readiness_lock:
            cached = self._readiness_cache
            if cached is not None and (now - cached[0]) < self._readiness_ttl:
                return cached[1]
        result = self._compute_readiness()
        with self._readiness_lock:
            self._readiness_cache = (time.monotonic(), result)
        return result

    def _compute_readiness(self) -> tuple[bool, dict[str, Any]]:
        checks: dict[str, Any] = {}
        ready = True
        try:
            self.store.ping()
            checks["store"] = "ok"
            checks["schema_version"] = self.store.schema_version()
        except Exception as exc:
            ready = False
            checks["store"] = safe_exception_summary(exc, "store readiness failed")
        queue_health = self.queue.health()
        queue_error = queue_health.get("last_error", "")
        if queue_error:
            queue_health = {
                **queue_health,
                "last_error": coerce_safe_summary(queue_error, "queue dependency failed"),
            }
        depth = self.queue.depth()
        checks["queue"] = queue_health
        if not queue_health["healthy"] or depth < 0:
            ready = False
        try:
            outbox = self.outbox.stats()
            checks["outbox"] = {
                "dispatcher_running": outbox["dispatcher_running"],
                "pending": outbox["pending"],
                "publishing": outbox["publishing"],
                "dead": outbox["dead"],
            }
            if not outbox["dispatcher_running"] or outbox["dead"]:
                ready = False
        except Exception as exc:
            checks["outbox"] = safe_exception_summary(exc, "outbox readiness failed")
            ready = False
        if self.settings.proof_require_remote:
            try:
                proof_health = self.proof_executor.health()
            except Exception:
                proof_health = {"healthy": False, "mode": "remote"}
            checks["proof_runner"] = proof_health
            if not proof_health.get("healthy"):
                ready = False
        return ready, {
            "status": "ready" if ready else "not-ready",
            "checks": checks,
            "queue_depth": depth,
            "queue_backend": self.queue.backend,
        }

    def _build_llm_reviewer(self, prompt: str = "") -> GatewayReviewer:
        return self.review_engine.build_llm_reviewer(prompt)

    def _candidate_reviewer(self, tenant_id: str):
        if not self.llm_config:
            return None
        deployment = self.store.get_deployment(tenant_id, "llm-review")
        if not deployment or deployment.get("candidate_version") is None:
            return None
        versions = self.store.list_skill_versions("llm-review")
        candidate = next(
            (
                item
                for item in versions
                if int(item["version"]) == int(deployment["candidate_version"])
            ),
            None,
        )
        return self._build_llm_reviewer(candidate["prompt"]) if candidate else None

    def _run_review(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        diff: str,
        tenant_id: str,
    ):
        task = self.store.get(task_id, tenant_id) or {}
        deployment = self.store.get_deployment(tenant_id, "llm-review")
        if (task.get("input") or {}).get("release_lane") == "canary" or (
            deployment and deployment.get("status") == "promoted"
        ):
            candidate = self._candidate_reviewer(tenant_id)
            if candidate:
                canary_reviewer = self.review_engine.build_coordinator(
                    [
                        item
                        for item in self.registry.reviewers()
                        if not isinstance(item, GatewayReviewer)
                    ]
                    + [candidate]
                )
                harness = self.review_engine.build_harness(canary_reviewer)
                return harness.run(task_id, repository, pull_request, diff)
        return self.harness.run(task_id, repository, pull_request, diff)

    def _run_shadow(
        self,
        task_id: str,
        tenant_id: str,
        diff: str,
        primary_report,
    ) -> None:
        task = self.store.get(task_id, tenant_id) or {}
        if not (task.get("input") or {}).get("shadow"):
            return
        candidate = self._candidate_reviewer(tenant_id)
        if not candidate:
            self.store.audit(
                tenant_id,
                "system",
                "shadow.skipped",
                task_id,
                {"reason": "candidate reviewer is unavailable"},
            )
            return
        lane = (task.get("input") or {}).get("release_lane", "stable")
        primary: dict[str, object] = {
            "risk": primary_report.risk,
            "finding_keys": sorted(item.fingerprint() for item in primary_report.findings),
        }
        try:
            parsed = parse_unified_diff(diff)
            findings = candidate.review_with_context(task_id, diff, parsed)
            candidate_result: dict[str, object] = {
                "finding_keys": sorted(item.fingerprint() for item in findings)
            }
            rollout = self.releases.observe_shadow(
                tenant_id, "llm-review", task_id, lane, primary, candidate_result
            )
            self.store.audit(
                tenant_id,
                "system",
                "shadow.completed",
                task_id,
                {
                    "findings": len(findings),
                    "candidate_output_used": False,
                    "rollout_status": (rollout or {}).get("status"),
                },
            )
            metrics.inc("shadow_reviews_total")
        except Exception as exc:
            error = safe_exception_summary(exc, "shadow review failed")
            self.releases.observe_shadow(
                tenant_id, "llm-review", task_id, lane, primary, None, True
            )
            self.store.audit(tenant_id, "system", "shadow.failed", task_id, {"error": error})
            metrics.inc("shadow_reviews_failed_total")

    def _record_session_turn(self, payload: dict[str, Any], report) -> str:
        return self.session_use_cases.record_review_turn(payload, report)

    @staticmethod
    def _continuity_note(summary: dict[str, int]) -> str:
        return SessionUseCases.continuity_note(summary)

    def get_session_timeline(
        self, session_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        return self.session_use_cases.get_timeline(session_id, tenant_id)

    def get_session_for_pull_request(
        self, repository: str, pull_request: int, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        return self.session_use_cases.get_for_pull_request(repository, pull_request, tenant_id)

    def provide_session_input(
        self, session_id: str, message: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        return self.session_use_cases.provide_input(session_id, message, tenant_id)

    def analyze_impact(self, sources: dict[str, Any], changed_paths: list[Any]) -> dict[str, Any]:
        return self.session_use_cases.analyze_impact(sources, changed_paths)

    def run_proof(
        self,
        original_files: dict[str, Any],
        patched_files: dict[str, Any],
        reproduction_command: str = "",
        regression_command: str = "",
    ) -> dict[str, Any]:
        return self.repair_use_cases.run_proof(
            original_files,
            patched_files,
            reproduction_command,
            regression_command,
        )

    def reload_skills(self) -> list:
        skills = self.review_engine.reload()
        self.registry = self.review_engine.registry
        self.reviewer = self.review_engine.reviewer
        self.harness = self.review_engine.harness
        self._publish_event("skills.reloaded", {"count": len(skills)})
        return skills

    def create_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        return self.review_use_cases.create_review(
            repository, diff, pull_request, source, tenant_id
        )

    def enqueue_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        github_issue_url: str = "",
        installation_id: int | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        return self.review_use_cases.enqueue_review(
            repository,
            diff,
            pull_request,
            source,
            github_issue_url,
            installation_id,
            tenant_id,
        )

    def _process_queued(self, payload: dict[str, Any]) -> None:
        self.review_use_cases.process_queued(payload)

    def _on_dead_letter(self, payload: dict[str, Any], error: str) -> None:
        self.review_use_cases.on_dead_letter(payload, error)

    def handle_github_pull_request(
        self,
        payload: dict[str, Any],
        delivery_id: str,
        payload_sha256: str,
        tenant_id: str = "",
    ) -> dict[str, Any]:
        return self.webhook_use_cases.handle_github_pull_request(
            payload,
            delivery_id,
            payload_sha256,
            tenant_id,
        )

    def github_client_for_installation(self, installation_id: int | None = None) -> CodeHostPort:
        if installation_id is None:
            return self.github
        if not self.settings.github_app_id or not self.settings.github_private_key_path:
            raise ValueError("GitHub App credentials are not configured")
        token = GitHubAppAuthenticator(
            self.settings.github_app_id, self.settings.github_private_key_path
        ).installation_token(installation_id)
        return GitHubClient(token, breaker=self.github_breaker)

    def create_fix(
        self,
        task_id: str,
        installation_id: int | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        # Preserve compatibility for callers/tests that replace the facade's
        # fixer after construction; the application object remains the owner of
        # the use-case implementation.
        self.repair_use_cases.fixer = self.fixer
        return self.repair_use_cases.create_fix(task_id, installation_id, tenant_id)

    def record_feedback(
        self,
        task_id: str,
        category: str,
        finding: dict | None,
        note: str,
        tenant_id: str | None = None,
    ) -> dict:
        return self.review_use_cases.record_feedback(task_id, category, finding, note, tenant_id)

    def resume_task(self, task_id: str, tenant_id: str | None = None) -> dict:
        return self.review_use_cases.resume_task(task_id, tenant_id)

    def cancel_task(self, task_id: str, tenant_id: str | None = None) -> bool:
        return self.review_use_cases.cancel_task(task_id, tenant_id)

    def get_repository_policy(self, tenant_id: str, repository: str) -> dict[str, Any]:
        return self.policy_use_cases.get_repository_policy(tenant_id, repository)

    def set_repository_policy(
        self,
        tenant_id: str,
        repository: str,
        policy: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        return self.policy_use_cases.set_repository_policy(tenant_id, repository, policy, actor)
