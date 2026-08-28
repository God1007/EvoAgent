import threading
import time
from collections.abc import Sequence
from typing import Any

from .agents import MultiAgentCoordinator, WorkflowFactory
from .application import (
    GitHubInstallationUseCases,
    PolicyUseCases,
    RepairOptions,
    RepairUseCases,
    ReviewOptions,
    ReviewUseCases,
    SessionUseCases,
    WebhookOptions,
    WebhookUseCases,
)
from .auth import Principal
from .backpressure import ConcurrencyLimiter, RateLimiter, TrustedProxyResolver
from .bootstrap import build_components, close_components
from .config import Settings
from .diff_parser import parse_unified_diff
from .errors import AccessDeniedError, ClientInputError, coerce_safe_summary, safe_exception_summary
from .github import GitHubAppAuthenticator, GitHubClient, GitHubInstallationOAuthClient
from .harness import ReviewHarness
from .metrics import metrics
from .outbox import OutboxDispatcher
from .ports import CodeHostPort
from .retention import RetentionManager, RetentionOptions
from .review_engine import ReviewEngine
from .review_extensions import ReviewerContribution
from .reviewer import GatewayReviewer, Reviewer
from .studio import WorkflowStudio
from .task_queue import PermanentTaskError, TaskQueue


class ReviewService:
    def console_capabilities(self, principal: Principal) -> dict[str, Any]:
        """Configuration hints only; never probe external services or expose credentials."""
        settings = self.settings
        return {
            "role": principal.role,
            "review": principal.can("review"),
            "manage": principal.can("manage"),
            "platform": principal.can("platform"),
            "github_install_configured": bool(
                settings.auth_required
                and settings.auth_secret
                and settings.github_app_slug
                and settings.github_app_id
                and settings.github_private_key_path
                and settings.github_client_id
                and settings.github_client_secret
                and settings.github_oauth_callback_url
                and settings.github_webhook_secret
            ),
        }

    def console_fix_blocker(self, task: dict, principal: Principal) -> str:
        """Explain known blockers without authorizing publication or touching GitHub."""
        if not principal.can("fix"):
            return "permission"
        task_input = task.get("input") or {}
        if (
            task.get("state") != "SUCCESS"
            or not task.get("report")
            or not task.get("pull_request")
            or not task_input.get("head_sha")
        ):
            return "pr_snapshot"
        policy = self.policies.resolve(principal.tenant_id, task["repository"])
        try:
            self.policies.authorize_fix(policy, tuple(getattr(self.fixer, "rule_ids", ())))
        except AccessDeniedError:
            return "policy"
        except ValueError:
            return "rules"
        installation = task_input.get("installation_id")
        if installation is not None:
            if (
                type(installation) is not int
                or installation < 1
                or self.store.installation_tenant(installation) != principal.tenant_id
            ):
                return "installation"
            credentials = self.settings.github_app_id and self.settings.github_private_key_path
        else:
            credentials = self.settings.github_token
        if not credentials:
            return "github"
        if not self.repair_container_image:
            return "sandbox"
        if not self.settings.repair_test_command.strip():
            return "tests"
        return ""

    def __init__(
        self,
        settings: Settings,
        *,
        workflow_factory: WorkflowFactory | None = None,
        reviewer_contributions: Sequence[ReviewerContribution] | None = None,
    ):
        self.settings = settings
        settings.validate_evolution()
        self.components = build_components(
            settings,
            workflow_factory=workflow_factory,
            reviewer_contributions=reviewer_contributions,
        )
        self._closed = False
        try:
            self.github_breaker = self.components.github_breaker
            self.llm_breaker = self.components.llm_breaker
            self.model_gateway = self.components.model_gateway
            self.store = self.components.store
            self.policies = self.components.policies
            self.observability = self.components.observability
            self.review_engine: ReviewEngine = self.components.review_engine
            self.llm_config = self.review_engine.llm_config
            self.github = self.components.github
            self.fixer = self.components.fixer
            self.proof_executor = self.components.proof_executor
            self.repair_container_image = self.components.repair_container_image
            self.auth = self.components.auth
            self.releases = self.components.releases
            self.alerts = self.components.alerts
            self.evolution = self.components.evolution
            self.studio = WorkflowStudio(
                self.store,
                lambda: (
                    self.reviewer.workflow_agents()
                    if isinstance(self.reviewer, MultiAgentCoordinator)
                    else {}
                ),
                self.model_gateway,
                self.review_engine._task_context,
            )
            self.github_installations = GitHubInstallationUseCases(
                self.store,
                self.auth,
                GitHubInstallationOAuthClient(
                    settings.github_client_id,
                    settings.github_client_secret,
                    settings.github_oauth_callback_url,
                ),
                settings.github_app_slug,
            )
            self.session_use_cases = SessionUseCases(self.store, settings.max_diff_bytes)
            self.policy_use_cases = PolicyUseCases(
                self.store,
                self.policies,
                lambda: tuple(getattr(self.fixer, "rule_ids", ())),
                lambda: (self.reviewer.name,),
            )
            self.repair_use_cases = RepairUseCases(
                self.store,
                self.policies,
                self.fixer,
                lambda installation_id: self.github_client_for_installation(installation_id),
                RepairOptions(
                    max_diff_bytes=settings.max_diff_bytes,
                    verify_timeout_seconds=settings.repair_verify_timeout_seconds,
                    container_image=self.components.repair_container_image,
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
                lambda task_id, repository, pull_request, diff, tenant_id, generation: (
                    self._run_review(task_id, repository, pull_request, diff, tenant_id, generation)
                ),
                lambda task_id, tenant_id, diff, report: self._run_shadow(
                    task_id, tenant_id, diff, report
                ),
                lambda payload, report: self._record_session_turn(payload, report),
                lambda installation_id: self.github_client_for_installation(installation_id),
                ReviewOptions(
                    max_diff_bytes=settings.max_diff_bytes,
                    effect_lease_seconds=settings.effect_lease_seconds,
                    auto_post_review=settings.auto_post_review,
                    tenant_max_active_reviews=settings.tenant_max_active_reviews,
                ),
                lambda tenant_id, repository: (
                    (self.model_gateway.route_info(),) if self.model_gateway.configured else None
                ),
                lambda: self.review_engine.execution_revision(),
                self.studio.select,
            )
            self.webhook_use_cases = WebhookUseCases(
                self.store,
                self.review_use_cases,
                lambda: self.queue,
                lambda: self.outbox.notify(),
                WebhookOptions(
                    default_tenant_id=settings.default_tenant_id,
                    auto_post_review=settings.auto_post_review,
                    require_installation_binding=bool(settings.github_client_id),
                ),
            )
            self.queue = TaskQueue(
                self._process_queued,
                settings.async_workers,
                settings.redis_url,
                settings.queue_max_attempts,
                settings.queue_lease_seconds,
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
                    webhook_replay_seconds=settings.webhook_max_age_seconds,
                    interval_seconds=settings.history_maintenance_seconds,
                    batch_size=settings.history_prune_batch_size,
                ),
            )
        except Exception:
            background_stopped = False
            try:
                background_stopped = self._stop_background(
                    time.monotonic() + settings.queue_shutdown_timeout_seconds
                )
            except Exception:
                pass
            if background_stopped:
                try:
                    close_components(self.components)
                except Exception:
                    pass
            raise
        metrics.register_gauge_source("queue_depth", self.queue.depth)
        metrics.register_gauge_source("queue_oldest_age_seconds", self.queue.oldest_age_seconds)
        metrics.register_gauge_source(
            "queue_healthy", lambda: 1.0 if self.queue.health()["healthy"] else 0.0
        )
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
        metrics.register_gauge_source(
            "outbox_dispatcher_running", lambda: 1.0 if self.outbox.running else 0.0
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

    @property
    def reviewer(self) -> Reviewer:
        return self.review_engine.execution_snapshot()[2].reviewer

    @property
    def harness(self) -> ReviewHarness:
        return self.review_engine.execution_snapshot()[2]

    def skill_inventory(self) -> tuple[list[dict], str]:
        return self.review_engine.inventory_snapshot()

    def close(self) -> None:
        """Release owned resources in reverse dependency order."""
        if self._closed:
            return
        deadline = time.monotonic() + self.settings.queue_shutdown_timeout_seconds
        background_stopped = False
        try:
            background_stopped = self._stop_background(deadline)
        finally:
            if background_stopped:
                try:
                    close_components(self.components)
                finally:
                    self._closed = True

    def _stop_background(self, deadline: float) -> bool:
        stopped = True
        first_error: Exception | None = None
        for name, timeout_metric in (
            ("retention", "retention_shutdown_timeouts_total"),
            ("outbox", "outbox_shutdown_timeouts_total"),
            ("queue", "queue_drain_timeouts_total"),
        ):
            resource = getattr(self, name, None)
            if resource is None:
                continue
            try:
                if not resource.close(max(0.0, deadline - time.monotonic())):
                    metrics.inc(timeout_metric)
                    stopped = False
            except Exception as exc:
                stopped = False
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
        return stopped

    def retention_status(self) -> dict[str, Any]:
        return self.retention.status()

    def review_admission_status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.tenant_max_active_reviews > 0,
            "max_active_reviews": self.settings.tenant_max_active_reviews,
            "retry_seconds": self.settings.tenant_capacity_retry_seconds,
        }

    def replay_outbox(self, message_id: str, tenant_id: str, actor: str) -> bool:
        if not message_id or len(message_id) > 200:
            raise ClientInputError("outbox message_id must contain 1-200 characters")
        replayed = self.store.requeue_outbox(message_id, tenant_id, actor)
        if replayed:
            self.outbox.notify()
        return replayed

    def tenant_dead_letters(self, tenant_id: str, limit: int) -> list[dict[str, Any]]:
        candidates = self.queue.dead_letters(500)
        task_ids = [
            str(payload["task_id"])
            for item in candidates
            if isinstance((payload := item.get("payload")), dict) and payload.get("task_id")
        ]
        owned = self.store.tenant_task_ids(tenant_id, task_ids)
        messages = []
        # ponytail: operational reads cap at 500; add a tenant-indexed durable DLQ if this is hot.
        for item in candidates:
            payload = item.get("payload")
            task_id = payload.get("task_id") if isinstance(payload, dict) else None
            if task_id and str(task_id) in owned:
                messages.append(item)
                if len(messages) >= min(max(1, limit), 500):
                    break
        return messages

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
        with self._readiness_lock:
            now = time.monotonic()
            cached = self._readiness_cache
            if cached is not None and (now - cached[0]) < self._readiness_ttl:
                return cached[1]
            result = self._compute_readiness()
            self._readiness_cache = (time.monotonic(), result)
            return result

    def _compute_readiness(self) -> tuple[bool, dict[str, Any]]:
        checks: dict[str, Any] = {}
        ready = True
        try:
            checks["schema_version"] = self.store.schema_version()
            checks["store"] = "ok"
        except Exception as exc:
            ready = False
            checks["store"] = safe_exception_summary(exc, "store readiness failed")
        try:
            queue_health = self.queue.health()
            queue_error = queue_health.get("last_error", "")
            if queue_error:
                queue_health = {
                    **queue_health,
                    "last_error": coerce_safe_summary(queue_error, "queue dependency failed"),
                }
            depth = self.queue.depth()
        except Exception as exc:
            ready = False
            depth = -1
            queue_health = {
                "healthy": False,
                "backend": self.queue.backend,
                "last_error": safe_exception_summary(exc, "queue dependency failed"),
            }
        checks["queue"] = queue_health
        if not queue_health["healthy"] or depth < 0:
            ready = False
        try:
            outbox = self.outbox.stats()
            outbox_error = outbox.get("last_error", "")
            checks["outbox"] = {
                "dispatcher_running": outbox["dispatcher_running"],
                "pending": outbox["pending"],
                "publishing": outbox["publishing"],
                "dead": outbox["dead"],
                "last_error": (
                    coerce_safe_summary(outbox_error, "outbox dispatch failed")
                    if outbox_error
                    else ""
                ),
            }
            if not outbox["dispatcher_running"] or outbox_error:
                ready = False
        except Exception as exc:
            checks["outbox"] = safe_exception_summary(exc, "outbox readiness failed")
            ready = False
        checks["circuit_breakers"] = {
            "github": self.github_breaker.state,
            "llm": self.llm_breaker.state,
        }
        container_configured = bool(getattr(self, "repair_container_image", ""))
        test_command_configured = bool(
            str(getattr(getattr(self, "settings", None), "repair_test_command", "")).strip()
        )
        checks["proof"] = {
            "configured": container_configured,
            "mode": "docker" if container_configured else "disabled",
        }
        repair_configured = container_configured and test_command_configured
        checks["repair"] = {
            "configured": repair_configured,
            "mode": "docker" if repair_configured else "disabled",
            "test_command_configured": test_command_configured,
        }
        return ready, {
            "status": "ready" if ready else "not-ready",
            "checks": checks,
            "queue_depth": depth,
            "queue_backend": self.queue.backend,
            "reviewer_revision": self.review_engine.execution_revision(),
        }

    def _build_llm_reviewer(self, prompt: str = "") -> GatewayReviewer:
        return self.review_engine.build_llm_reviewer(prompt)

    def _versioned_reviewer(self, version: int | None):
        if not self.llm_config or version is None:
            return None
        selected = self.store.get_skill_version("llm-review", version)
        return self._build_llm_reviewer(selected["prompt"]) if selected else None

    def _run_review(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        diff: str,
        tenant_id: str,
        admission_generation: int | None = None,
    ):
        task = self.store.get(task_id, tenant_id) or {}
        task_input = task.get("input") or {}
        revision, base_reviewers, default_harness = self.review_engine.execution_snapshot()
        expected_revision = task_input.get("reviewer_revision")
        if not isinstance(expected_revision, str) or expected_revision != revision:
            self.store.audit(
                tenant_id,
                "system",
                "reviewer.revision_mismatch",
                task_id,
                {"expected": expected_revision, "current": revision},
            )
            raise PermanentTaskError("assigned reviewer revision is unavailable")
        if "studio_workflow" in task_input:
            snapshot = task_input["studio_workflow"]
            if not isinstance(snapshot, dict):
                raise PermanentTaskError("assigned workflow snapshot is invalid")
            return self.review_engine.build_studio_harness(snapshot).run(
                task_id, repository, pull_request, diff, admission_generation
            )
        lane = task_input.get("release_lane", "stable")
        shadow = task_input.get("shadow")
        stable_version = task_input.get("release_stable_version")
        candidate_version = task_input.get("release_candidate_version")
        generation = task_input.get("release_generation")
        versions = (stable_version, candidate_version, generation)
        if (
            not isinstance(shadow, bool)
            or lane not in {"stable", "canary"}
            or any(
                value is not None
                and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 1 <= value <= 2**31 - 1
                )
                for value in versions
            )
            or (candidate_version is None) != (generation is None)
            or (lane == "canary" and candidate_version is None)
        ):
            raise PermanentTaskError("assigned release snapshot is invalid")
        deployment = self.store.get_deployment(tenant_id, "llm-review")
        status = (deployment or {}).get("status")
        if stable_version is None and status in {"promoted", "rolled_back"}:
            stable_version = (deployment or {}).get("stable_version")
        candidate_is_current = (
            candidate_version is not None
            and generation is not None
            and (deployment or {}).get("generation") == generation
            and (
                (
                    status == "running"
                    and (deployment or {}).get("candidate_version") == candidate_version
                )
                or (
                    status == "promoted"
                    and (deployment or {}).get("stable_version") == candidate_version
                )
            )
        )
        selected_version = (
            candidate_version if lane == "canary" and candidate_is_current else stable_version
        )
        if lane == "canary" and candidate_version is not None and not candidate_is_current:
            self.store.audit(
                tenant_id,
                "system",
                "canary.skipped",
                task_id,
                {
                    "reason": "assigned candidate is no longer active",
                    "candidate_version": candidate_version,
                    "generation": generation,
                },
            )
        reviewer = self._versioned_reviewer(selected_version)
        if selected_version is not None:
            if reviewer is None:
                raise RuntimeError("assigned LLM review version is unavailable")
            versioned_reviewer = self.review_engine.build_coordinator(
                [item for item in base_reviewers if not isinstance(item, GatewayReviewer)]
                + [reviewer]
            )
            harness = self.review_engine.build_harness(versioned_reviewer)
            return harness.run(task_id, repository, pull_request, diff, admission_generation)
        return default_harness.run(task_id, repository, pull_request, diff, admission_generation)

    def _run_shadow(
        self,
        task_id: str,
        tenant_id: str,
        diff: str,
        primary_report,
    ) -> None:
        task = self.store.get(task_id, tenant_id) or {}
        task_input = task.get("input") or {}
        if task_input.get("studio_workflow"):
            return
        if task_input.get("shadow") is not True:
            return
        if not self.policies.resolve(tenant_id, str(task.get("repository") or "")).enabled:
            self.store.audit(
                tenant_id,
                "system",
                "shadow.skipped",
                task_id,
                {"reason": "repository was disabled after task acceptance"},
            )
            return
        deployment = self.store.get_deployment(tenant_id, "llm-review")
        candidate_version = task_input.get("release_candidate_version")
        generation = task_input.get("release_generation")
        if (
            (deployment or {}).get("status") != "running"
            or (deployment or {}).get("candidate_version") != candidate_version
            or (deployment or {}).get("generation") != generation
        ):
            self.store.audit(
                tenant_id,
                "system",
                "shadow.skipped",
                task_id,
                {
                    "reason": "assigned candidate is no longer active",
                    "candidate_version": candidate_version,
                    "generation": generation,
                },
            )
            return
        lane = task_input.get("release_lane", "stable")
        primary: dict[str, object] = {
            "risk": primary_report.risk,
            "finding_keys": sorted(item.fingerprint() for item in primary_report.findings),
        }
        try:
            _revision, base_reviewers, _harness = self.review_engine.execution_snapshot()
            stable_version = task_input.get("release_stable_version")
            shadow_version = stable_version if lane == "canary" else candidate_version
            shadow_model = (
                self._versioned_reviewer(shadow_version)
                if shadow_version is not None
                else next(
                    (item for item in base_reviewers if isinstance(item, GatewayReviewer)), None
                )
            )
            if shadow_model is None:
                raise RuntimeError("shadow reviewer is unavailable")
            parsed = parse_unified_diff(diff)
            shadow_reviewer = self.review_engine.build_coordinator(
                [item for item in base_reviewers if not isinstance(item, GatewayReviewer)]
                + [shadow_model],
                persist_messages=False,
            )
            findings = shadow_reviewer.review_with_context(task_id, diff, parsed)
            shadow_result: dict[str, object] = {
                "finding_keys": sorted(item.fingerprint() for item in findings),
                "version": shadow_version,
                "generation": generation,
            }
            baseline, candidate_result = (
                (
                    shadow_result,
                    {**primary, "version": candidate_version, "generation": generation},
                )
                if lane == "canary"
                else (primary, shadow_result)
            )
            audit_detail: dict[str, object] = {
                "findings": len(findings),
                "candidate_output_used": False,
            }
            rollout = self.releases.observe_shadow(
                tenant_id,
                "llm-review",
                task_id,
                lane,
                baseline,
                candidate_result,
                candidate_version=candidate_version,
                generation=generation,
                audit_event=("shadow.completed", audit_detail),
            )
            if rollout is None:
                self.store.audit(
                    tenant_id,
                    "system",
                    "shadow.completed",
                    task_id,
                    {**audit_detail, "rollout_status": None},
                )
            metrics.inc("shadow_reviews_total")
        except Exception as exc:
            error = safe_exception_summary(exc, "shadow review failed")
            audited = False
            if lane == "stable":
                rollout = self.releases.observe_shadow(
                    tenant_id,
                    "llm-review",
                    task_id,
                    lane,
                    primary,
                    None,
                    candidate_failed=True,
                    candidate_version=candidate_version,
                    generation=generation,
                    audit_event=("shadow.failed", {"error": error}),
                )
                audited = rollout is not None
            if not audited:
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
        self,
        session_id: str,
        message: str,
        tenant_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        return self.session_use_cases.provide_input(session_id, message, tenant_id, actor)

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

    def create_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        tenant_id: str = "default",
        actor: str = "",
        workflow_selection: dict | None = None,
    ) -> dict[str, Any]:
        return self.review_use_cases.create_review(
            repository, diff, pull_request, source, tenant_id, actor, workflow_selection
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
        idempotency_key: str = "",
        actor: str = "",
        workflow_selection: dict | None = None,
    ) -> dict[str, Any]:
        return self.review_use_cases.enqueue_review(
            repository,
            diff,
            pull_request,
            source,
            github_issue_url,
            installation_id,
            tenant_id,
            idempotency_key,
            actor,
            workflow_selection,
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

    def begin_github_installation(self, principal: Principal) -> str:
        return self.github_installations.begin(principal)

    def authorize_github_installation(self, state: str, installation_id: str) -> str:
        return self.github_installations.authorize(state, installation_id)

    def complete_github_installation(self, state: str, code: str) -> dict[str, object]:
        return self.github_installations.complete(state, code)

    def github_client_for_installation(self, installation_id: int | None = None) -> CodeHostPort:
        if installation_id is None:
            return self.github
        if not self.settings.github_app_id or not self.settings.github_private_key_path:
            raise ValueError("GitHub App credentials are not configured")
        authenticator = GitHubAppAuthenticator(
            self.settings.github_app_id,
            self.settings.github_private_key_path,
            breaker=self.github_breaker,
        )
        token = authenticator.installation_token(installation_id)
        return GitHubClient(
            token,
            breaker=self.github_breaker,
            max_archive_bytes=self.settings.repair_memory_mb * 1024 * 1024,
            on_unauthorized=lambda stale: authenticator.invalidate(installation_id, stale),
            comment_author_login=(
                self.settings.github_app_slug + "[bot]" if self.settings.github_app_slug else ""
            ),
        )

    def create_fix(
        self,
        task_id: str,
        tenant_id: str | None = None,
        actor: str = "system",
    ) -> dict:
        # Preserve compatibility for callers/tests that replace the facade's
        # fixer after construction; the application object remains the owner of
        # the use-case implementation.
        self.repair_use_cases.fixer = self.fixer
        return self.repair_use_cases.create_fix(task_id, tenant_id, actor)

    def record_feedback(
        self,
        task_id: str,
        category: str,
        finding: dict | None,
        note: str,
        tenant_id: str | None = None,
        actor: str = "",
    ) -> dict:
        return self.review_use_cases.record_feedback(
            task_id, category, finding, note, tenant_id, actor
        )

    def resume_task(self, task_id: str, tenant_id: str, actor: str = "system") -> dict:
        return self.review_use_cases.resume_task(task_id, tenant_id, actor)

    def cancel_task(self, task_id: str, tenant_id: str, actor: str = "system") -> dict[str, Any]:
        return self.review_use_cases.cancel_task(task_id, tenant_id, actor)

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
