"""Review admission, execution, delivery, and task lifecycle use cases."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import (
    AccessDeniedError,
    ClientInputError,
    TenantReviewCapacityError,
    coerce_safe_summary,
    safe_exception_fields,
    safe_exception_summary,
)
from ..metrics import metrics
from ..models import TaskState, TraceEvent
from ..policy import RepositoryPolicy, RepositoryPolicyResolver
from ..ports import (
    CodeHostPort,
    ObservabilityPort,
    ReviewAlertPort,
    ReviewApplicationStorePort,
    ReviewReleasePort,
    TaskQueuePort,
)
from ..report import to_markdown
from ..store import utc_now
from ..task_queue import PermanentTaskError


@dataclass(frozen=True)
class ReviewOptions:
    max_diff_bytes: int
    queue_lease_seconds: float
    auto_post_review: bool
    tenant_max_active_reviews: int = 0


class ReviewUseCases:
    """Application boundary for every review-task state transition.

    Infrastructure is injected through narrow ports and callbacks. The class
    owns orchestration and policy decisions, while the compatibility service
    owns only runtime composition and adapter lifecycle.
    """

    def __init__(
        self,
        store: ReviewApplicationStorePort,
        policies: RepositoryPolicyResolver,
        releases: ReviewReleasePort,
        alerts: ReviewAlertPort,
        observability: ObservabilityPort,
        queue: Callable[[], TaskQueuePort],
        notify_outbox: Callable[[], None],
        reviewer_identity: Callable[[], tuple[str, str, str]],
        execute_review: Callable[[str, str, int | None, str, str], Any],
        run_shadow: Callable[[str, str, str, Any], None],
        record_session_turn: Callable[[dict[str, Any], Any], str],
        code_host_for_installation: Callable[[int | None], CodeHostPort],
        publish_event: Callable[[str, dict[str, Any]], None],
        options: ReviewOptions,
        model_routes: Callable[[str, str], tuple[dict[str, str], ...] | None] | None = None,
    ):
        self.store = store
        self.policies = policies
        self.releases = releases
        self.alerts = alerts
        self.observability = observability
        self.queue = queue
        self.notify_outbox = notify_outbox
        self.reviewer_identity = reviewer_identity
        self.execute_review = execute_review
        self.run_shadow = run_shadow
        self.record_session_turn = record_session_turn
        self.code_host_for_installation = code_host_for_installation
        self.publish_event = publish_event
        self.options = options
        self.model_routes = model_routes or (lambda _tenant_id, _repository: None)

    def validate(self, repository: str, diff: str) -> None:
        if not repository or len(repository) > 250:
            raise ClientInputError("repository is required and must be at most 250 characters")
        size = len(diff.encode("utf-8"))
        if size == 0:
            raise ClientInputError("diff is required")
        if size > self.options.max_diff_bytes:
            raise ClientInputError(
                "diff exceeds maximum size of %d bytes" % self.options.max_diff_bytes
            )

    def authorize_review(self, tenant_id: str, repository: str, diff: str) -> RepositoryPolicy:
        self.validate(repository, diff)
        policy = self.policies.resolve(tenant_id, repository)
        reviewer, provider, model = self.reviewer_identity()
        self.policies.authorize_review(
            policy,
            len(diff.encode("utf-8")),
            reviewer,
            provider,
            model,
            self.model_routes(tenant_id, repository),
        )
        return policy

    def authorize_repository(self, tenant_id: str, repository: str) -> RepositoryPolicy:
        policy = self.policies.resolve(tenant_id, repository)
        if not policy.enabled:
            raise AccessDeniedError("repository is not authorized for this tenant")
        return policy

    def create_task(
        self,
        repository: str,
        diff: str,
        pull_request: int | None,
        source: str,
        tenant_id: str = "default",
        outbox_payload: dict[str, Any] | None = None,
        policy: RepositoryPolicy | None = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        try:
            self.store.create_review_task(
                task_id,
                repository,
                pull_request,
                {
                    "source": source,
                    "diff_bytes": len(encoded),
                    "diff_sha256": hashlib.sha256(encoded).hexdigest(),
                    "release_lane": assignment["lane"],
                    "shadow": assignment["shadow"],
                    "repository_policy": self.policies.snapshot(
                        policy or self.policies.resolve(tenant_id, repository)
                    ),
                },
                tenant_id,
                diff,
                {"task_id": task_id, **outbox_payload} if outbox_payload is not None else None,
                self.options.tenant_max_active_reviews,
            )
        except TenantReviewCapacityError:
            metrics.inc("review_admission_rejections_total")
            raise
        metrics.inc("review_admissions_total")
        return task_id

    def create_deferred_task(
        self,
        repository: str,
        pull_request: int | None,
        source: str,
        tenant_id: str,
        payload: dict[str, Any],
        outbox_payload: dict[str, Any],
        policy: RepositoryPolicy | None = None,
    ) -> str:
        task_id, prepared_payload = self.prepare_deferred_task(
            repository, source, tenant_id, payload, policy
        )
        try:
            self.store.create_review_task(
                task_id,
                repository,
                pull_request,
                prepared_payload,
                tenant_id,
                None,
                {"task_id": task_id, **outbox_payload},
                self.options.tenant_max_active_reviews,
            )
        except TenantReviewCapacityError:
            metrics.inc("review_admission_rejections_total")
            raise
        metrics.inc("review_admissions_total")
        return task_id

    def prepare_deferred_task(
        self,
        repository: str,
        source: str,
        tenant_id: str,
        payload: dict[str, Any],
        policy: RepositoryPolicy | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Prepare immutable task input for a caller-owned unit of work."""
        task_id = str(uuid.uuid4())
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        return (
            task_id,
            {
                "source": source,
                "diff_pending": True,
                "release_lane": assignment["lane"],
                "shadow": assignment["shadow"],
                "repository_policy": self.policies.snapshot(
                    policy or self.policies.resolve(tenant_id, repository)
                ),
                **payload,
            },
        )

    def create_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        policy = self.authorize_review(tenant_id, repository, diff)
        task_id = self.create_task(repository, diff, pull_request, source, tenant_id, policy=policy)
        self._review_started(task_id, tenant_id, repository, source, "synchronous")
        try:
            with (
                self.observability.span(
                    "review",
                    task_id,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    repository=repository,
                ),
                metrics.latency("review_duration"),
            ):
                report = self.execute_review(task_id, repository, pull_request, diff, tenant_id)
            self.run_shadow(task_id, tenant_id, diff, report)
            self._review_succeeded(task_id, tenant_id, repository, report, "synchronous")
            return {"task_id": task_id, "state": "SUCCESS", "report": report.to_dict()}
        except Exception as exc:
            self._review_failed(task_id, tenant_id, repository, exc, "synchronous")
            raise

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
        policy = self.authorize_review(tenant_id, repository, diff)
        task_id = self.create_task(
            repository,
            diff,
            pull_request,
            source,
            tenant_id,
            {
                "repository": repository,
                "pull_request": pull_request,
                "github_issue_url": github_issue_url,
                "installation_id": installation_id,
                "tenant_id": tenant_id,
            },
            policy,
        )
        self.notify_outbox()
        metrics.inc("reviews_enqueued_total")
        return {"task_id": task_id, "state": "PENDING", "queue": self.queue().backend}

    def process_queued(self, payload: dict[str, Any]) -> None:
        task_id = payload["task_id"]
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        if task.get("state") in {TaskState.SUCCESS.value, TaskState.CANCELLED.value}:
            # Redis Streams is at-least-once and recovery deliberately rebuilds
            # intent. A late duplicate must ACK as a no-op before shadow runs,
            # release accounting, or external publication are repeated.
            metrics.inc("queue_terminal_duplicates_total")
            return
        tenant_id = payload.get("tenant_id") or task.get("tenant_id") or "default"
        repository = payload["repository"]
        task_input = task.get("input") or {}
        snapshot = task_input.get("repository_policy")
        policy = (
            self.policies.from_snapshot(snapshot)
            if snapshot is not None
            else self.policies.resolve(tenant_id, repository)
        )
        current_policy = self.policies.resolve(tenant_id, repository)
        if not current_policy.enabled:
            raise AccessDeniedError("repository was disabled after this task was accepted")
        diff = self.store.get_task_payload(task_id)
        fetched_diff = False
        if diff is None and payload.get("diff_url"):
            client = self.code_host_for_installation(payload.get("installation_id"))
            client.ensure_repository_access(repository)
            diff = client.fetch_diff(
                payload["diff_url"], max_bytes=self.options.max_diff_bytes + 4096
            )
            fetched_diff = True
        if diff is None:
            raise PermanentTaskError("task payload no longer exists")
        self.validate(repository, diff)
        reviewer, provider, model = self.reviewer_identity()
        self.policies.authorize_review(
            policy,
            len(diff.encode("utf-8")),
            reviewer,
            provider,
            model,
            self.model_routes(tenant_id, repository),
        )
        if fetched_diff:
            encoded = diff.encode("utf-8")
            self.store.save_task_payload(task_id, diff)
            self.store.update_task_input(
                task_id,
                {
                    "diff_pending": False,
                    "diff_bytes": len(encoded),
                    "diff_sha256": hashlib.sha256(encoded).hexdigest(),
                },
            )
        self._review_started(task_id, tenant_id, repository, "queue", "asynchronous")
        try:
            with (
                self.observability.span(
                    "review.async", task_id, task_id=task_id, tenant_id=tenant_id
                ),
                metrics.latency("review_duration"),
            ):
                report = self.execute_review(
                    task_id,
                    repository,
                    payload.get("pull_request"),
                    diff,
                    tenant_id,
                )
            self.run_shadow(task_id, tenant_id, diff, report)
            self._review_succeeded(task_id, tenant_id, repository, report, "asynchronous")
            continuity = self.record_session_turn(payload, report)
            if (
                payload.get("github_issue_url")
                and self.options.auto_post_review
                and policy.post_review_comments
                and current_policy.post_review_comments
            ):
                self._post_review_comment(payload, task_id, report, continuity)
        except Exception as exc:
            self._review_failed(task_id, tenant_id, repository, exc, "asynchronous")
            raise

    def _post_review_comment(
        self,
        payload: dict[str, Any],
        task_id: str,
        report: Any,
        continuity: str,
    ) -> None:
        client = self.code_host_for_installation(payload.get("installation_id"))
        body = to_markdown(report.to_dict())
        if continuity:
            body += "\n\n" + continuity
        marker = (
            "<!-- evoagent-session:%s -->" % payload["session_id"]
            if payload.get("session_id")
            else "<!-- evoagent-review:%s -->" % task_id
        )
        effect_key = (
            "github-comment:"
            + hashlib.sha256(
                (payload["github_issue_url"] + "\n" + marker).encode("utf-8")
            ).hexdigest()
        )
        owner = uuid.uuid4().hex
        receipt = self.store.claim_effect(
            effect_key,
            owner,
            max(1.0, min(15.0, self.options.queue_lease_seconds / 2)),
        )
        if receipt["status"] == "acquired":
            try:
                client.upsert_comment(payload["github_issue_url"], body, marker)
                self.store.complete_effect(effect_key, owner, {"marker": marker})
            except Exception as exc:
                self.store.release_effect(
                    effect_key,
                    owner,
                    safe_exception_summary(exc, "external effect failed"),
                )
                raise
        elif receipt["status"] == "busy":
            metrics.inc("effect_receipt_busy_total")

    def _review_started(
        self, task_id: str, tenant_id: str, repository: str, source: str, mode: str
    ) -> None:
        self.publish_event(
            "review.started",
            {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "repository": repository,
                "source": source,
                "mode": mode,
            },
        )

    def _review_succeeded(
        self,
        task_id: str,
        tenant_id: str,
        repository: str,
        report: Any,
        mode: str,
    ) -> None:
        metrics.inc("reviews_total")
        metrics.inc("review_admission_releases_total")
        persisted = self.store.get(task_id, tenant_id) or {}
        lane = (persisted.get("input") or {}).get("release_lane", "stable")
        self.releases.observe(tenant_id, "llm-review", False, lane)
        self.publish_event(
            "review.completed",
            {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "repository": repository,
                "risk": report.risk,
                "findings": len(report.findings),
                "mode": mode,
            },
        )

    def _review_failed(
        self,
        task_id: str,
        tenant_id: str,
        repository: str,
        error: Exception,
        mode: str,
    ) -> None:
        metrics.inc("reviews_failed_total")
        if mode == "synchronous":
            metrics.inc("review_admission_releases_total")
        task = self.store.get(task_id, tenant_id) or {}
        lane = (task.get("input") or {}).get("release_lane", "stable")
        self.releases.observe(tenant_id, "llm-review", True, lane)
        self.alerts.evaluate(tenant_id)
        failure = safe_exception_fields(error)
        self.publish_event(
            "review.failed",
            {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "repository": repository,
                "error_type": failure["error_type"],
                "error_ref": failure["error_ref"],
                "mode": mode,
            },
        )

    def on_dead_letter(self, payload: dict[str, Any], error: str) -> None:
        error = coerce_safe_summary(error, "task delivery failed")
        task_id = payload.get("task_id", "")
        tenant_id = payload.get("tenant_id", "default")
        task = self.store.get(task_id, tenant_id) if task_id else None
        if task and task.get("state") not in {
            TaskState.SUCCESS.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            step = max([int(item.get("step", 0)) for item in task.get("trace", [])] or [0]) + 1
            self.store.fail(
                task_id,
                error,
                TraceEvent(
                    step,
                    TaskState.FAILED,
                    error,
                    utc_now(),
                ),
            )
        raw_generation = payload.get("admission_generation")
        try:
            generation = int(raw_generation) if raw_generation is not None else None
        except (TypeError, ValueError):
            generation = -1
        if task_id and self.store.release_review_admission(task_id, "dead-letter", generation):
            metrics.inc("review_admission_releases_total")
        self.store.create_alert(
            tenant_id,
            "dlq:%s" % (task_id or "unknown"),
            "critical",
            error,
        )
        metrics.inc("dead_letters_total")
        self.publish_event(
            "task.dead-lettered",
            {"task_id": task_id, "tenant_id": tenant_id, "error": error[:500]},
        )

    def record_feedback(
        self,
        task_id: str,
        category: str,
        finding: dict[str, Any] | None,
        note: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.store.get(task_id, tenant_id):
            raise ClientInputError("task not found")
        if category not in {"false_positive", "missed_issue", "bad_fix", "accepted"}:
            raise ClientInputError("unsupported feedback category")
        self.store.record_failure_case(task_id, category, {"finding": finding, "note": note[:2000]})
        metrics.inc("feedback_total")
        metrics.inc("feedback_%s_total" % category)
        return {"recorded": True, "category": category}

    def resume_task(self, task_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ClientInputError("task not found")
        if task["state"] == "SUCCESS":
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if self.store.get_task_payload(task_id) is None:
            raise ClientInputError("task payload is no longer available")
        actual_tenant = str(task.get("tenant_id") or tenant_id or "default")
        resume_id = uuid.uuid4().hex
        payload = {
            "task_id": task_id,
            "repository": task["repository"],
            "pull_request": task.get("pull_request"),
            "tenant_id": actual_tenant,
        }
        try:
            result = self.store.resume_review_task(
                task_id,
                actual_tenant,
                self.options.tenant_max_active_reviews,
                "review-resume:" + resume_id,
                "review-resume:" + resume_id,
                payload,
            )
        except TenantReviewCapacityError:
            metrics.inc("review_admission_rejections_total")
            raise
        status = result["status"]
        if status == "missing":
            raise ClientInputError("task not found")
        if status == "cancelled":
            raise ClientInputError("cancelled task cannot be resumed")
        if status == "success":
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if status == "resumed":
            metrics.inc("review_admissions_total")
            self.notify_outbox()
        return {
            "task_id": task_id,
            "state": "PENDING",
            "resumed": status == "resumed",
            "already_active": status == "active",
        }

    def tenant_capacity_report(self, tenant_id: str) -> dict[str, Any]:
        stats = self.store.tenant_review_admission_stats(tenant_id)
        limit = self.options.tenant_max_active_reviews
        active = int(stats["active"])
        return {
            "tenant_id": tenant_id,
            "enabled": limit > 0,
            "max_active_reviews": limit,
            "active_reviews": active,
            "available": max(0, limit - active) if limit else None,
            "saturated": bool(limit and active >= limit),
            "oldest_acquired_at": stats["oldest_acquired_at"],
        }

    def cancel_task(self, task_id: str, tenant_id: str | None = None) -> bool:
        return self.store.request_cancel(task_id, tenant_id)
