"""Review admission, execution, delivery, and task lifecycle use cases."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import (
    AccessDeniedError,
    ClientInputError,
    ResourceNotFoundError,
    StateConflictError,
    TenantReviewCapacityError,
    coerce_safe_summary,
    safe_exception_summary,
)
from ..metrics import metrics
from ..models import ReviewReport, TaskState, TraceEvent
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
from ..repository import canonical_repository
from ..task_queue import PermanentTaskError
from ..time_utils import utc_now

_QUEUE_CONTEXT_FIELDS = (
    "diff_url",
    "github_issue_url",
    "installation_id",
    "session_id",
    "turn_id",
    "head_sha",
    "trigger",
)
# ponytail: leave headroom below provider limits; add artifact links only if truncation is common.
_COMMENT_MAX_CHARS = 60_000


class _CommentSuppressed(Exception):
    pass


@dataclass(frozen=True)
class ReviewOptions:
    max_diff_bytes: int
    effect_lease_seconds: float
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
        execute_review: Callable[[str, str, int | None, str, str, int | None], Any],
        run_shadow: Callable[[str, str, str, Any], None],
        record_session_turn: Callable[[dict[str, Any], Any], str],
        code_host_for_installation: Callable[[int | None], CodeHostPort],
        options: ReviewOptions,
        model_routes: Callable[[str, str], tuple[dict[str, str], ...] | None] | None = None,
        reviewer_revision: Callable[[], str] | None = None,
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
        self.options = options
        self.model_routes = model_routes or (lambda _tenant_id, _repository: None)
        self.reviewer_revision = reviewer_revision or (lambda: "")

    def validate(self, repository: str, diff: str) -> None:
        canonical_repository(repository)
        if not isinstance(diff, str):
            raise ClientInputError("diff must be a string")
        try:
            size = len(diff.encode("utf-8"))
        except UnicodeEncodeError:
            raise ClientInputError("diff must be valid UTF-8") from None
        if size == 0:
            raise ClientInputError("diff is required")
        if size > self.options.max_diff_bytes:
            raise ClientInputError(
                "diff exceeds maximum size of %d bytes" % self.options.max_diff_bytes
            )

    @staticmethod
    def validate_pull_request(pull_request: int | None) -> None:
        if pull_request is not None and (
            not isinstance(pull_request, int)
            or isinstance(pull_request, bool)
            or not 1 <= pull_request <= 2**31 - 1
        ):
            raise ClientInputError("pull_request must be a positive PostgreSQL integer")

    @staticmethod
    def _release_snapshot(assignment: dict[str, Any]) -> dict[str, Any]:
        deployment = assignment.get("deployment") or {}
        return {
            "release_lane": assignment["lane"],
            "shadow": assignment["shadow"],
            "release_stable_version": deployment.get("stable_version"),
            "release_candidate_version": deployment.get("candidate_version"),
            "release_generation": deployment.get("generation"),
        }

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
        idempotency_key: str = "",
        actor: str = "",
    ) -> tuple[str, bool]:
        repository = canonical_repository(repository)
        self.validate_pull_request(pull_request)
        encoded = diff.encode("utf-8")
        diff_sha256 = hashlib.sha256(encoded).hexdigest()
        task_id = (
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    json.dumps(
                        [tenant_id, idempotency_key],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
            if idempotency_key
            else str(uuid.uuid4())
        )
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        task_payload = {
            "source": source,
            "diff_bytes": len(encoded),
            "diff_sha256": diff_sha256,
            "reviewer_revision": self.reviewer_revision(),
            **self._release_snapshot(assignment),
            "repository_policy": self.policies.snapshot(
                policy or self.policies.resolve(tenant_id, repository)
            ),
        }
        if outbox_payload is not None:
            task_payload.update(
                {key: outbox_payload[key] for key in _QUEUE_CONTEXT_FIELDS if key in outbox_payload}
            )
        if idempotency_key:
            task_payload["idempotency_fingerprint"] = hashlib.sha256(
                json.dumps(
                    {
                        "repository": repository,
                        "pull_request": pull_request,
                        "source": source,
                        "diff_sha256": diff_sha256,
                        "context": {
                            key: outbox_payload[key]
                            for key in _QUEUE_CONTEXT_FIELDS
                            if outbox_payload is not None and key in outbox_payload
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        try:
            created = (
                self.store.create_review_task(
                    task_id,
                    repository,
                    pull_request,
                    task_payload,
                    tenant_id,
                    diff,
                    {"task_id": task_id, **outbox_payload} if outbox_payload is not None else None,
                    self.options.tenant_max_active_reviews,
                    actor,
                )
                is not False
            )
        except TenantReviewCapacityError:
            metrics.inc("review_admission_rejections_total")
            raise
        metrics.inc("review_admissions_total" if created else "review_idempotent_replays_total")
        return task_id, created

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
        repository = canonical_repository(repository)
        self.validate_pull_request(pull_request)
        task_id, prepared_payload = self.prepare_deferred_task(
            repository, source, tenant_id, payload, policy
        )
        prepared_payload.update(
            {key: outbox_payload[key] for key in _QUEUE_CONTEXT_FIELDS if key in outbox_payload}
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
        repository = canonical_repository(repository)
        task_id = str(uuid.uuid4())
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        return (
            task_id,
            {
                "source": source,
                "diff_pending": True,
                "reviewer_revision": self.reviewer_revision(),
                **self._release_snapshot(assignment),
                "repository_policy": self.policies.snapshot(
                    policy or self.policies.resolve(tenant_id, repository)
                ),
                **payload,
            },
        )

    def _run_shadow_safely(
        self, task_id: str, tenant_id: str, diff: str, report: ReviewReport
    ) -> None:
        try:
            self.run_shadow(task_id, tenant_id, diff, report)
        except Exception as exc:
            metrics.inc("shadow_reviews_failed_total")
            try:
                self.store.audit(
                    tenant_id,
                    "system",
                    "shadow.failed",
                    task_id,
                    {"error": safe_exception_summary(exc, "shadow review failed")},
                )
            except Exception:
                metrics.inc("shadow_audit_failures_total")

    def create_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        tenant_id: str = "default",
        actor: str = "",
    ) -> dict[str, Any]:
        repository = canonical_repository(repository)
        policy = self.authorize_review(tenant_id, repository, diff)
        task_id, _created = self.create_task(
            repository, diff, pull_request, source, tenant_id, policy=policy, actor=actor
        )
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
                report = self.execute_review(task_id, repository, pull_request, diff, tenant_id, 1)
            self._run_shadow_safely(task_id, tenant_id, diff, report)
            self._review_succeeded(task_id, tenant_id)
            return {"task_id": task_id, "state": "SUCCESS", "report": report.to_dict()}
        except Exception:
            self._account_synchronous_exception(task_id, tenant_id)
            raise

    def _account_synchronous_exception(self, task_id: str, tenant_id: str) -> None:
        try:
            task = self.store.get(task_id, tenant_id) or {}
        except Exception:
            metrics.inc("review_terminal_accounting_failures_total")
            return
        state = task.get("state")
        if state == TaskState.FAILED.value:
            self._review_failed(task_id, tenant_id, "synchronous")
        elif state == TaskState.CANCELLED.value:
            metrics.inc("reviews_cancelled_total")
            metrics.inc("review_admission_releases_total")
        else:
            metrics.inc("review_terminal_accounting_failures_total")

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
    ) -> dict[str, Any]:
        repository = canonical_repository(repository)
        policy = self.authorize_review(tenant_id, repository, diff)
        task_id, created = self.create_task(
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
            idempotency_key,
            actor,
        )
        self.notify_outbox()
        if created:
            metrics.inc("reviews_enqueued_total")
        task = self.store.get(task_id, tenant_id) if not created else None
        state = str(task.get("state")) if task else TaskState.PENDING.value
        return {
            "task_id": task_id,
            "state": state,
            "queue": self.queue().backend,
            "replayed": not created,
        }

    def process_queued(self, payload: dict[str, Any]) -> None:
        task_id = payload["task_id"]
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        if task.get("state") == TaskState.CANCELLED.value:
            # Redis Streams is at-least-once and recovery deliberately rebuilds
            # intent. A late duplicate must ACK as a no-op before shadow runs,
            # release accounting, or external publication are repeated.
            metrics.inc("queue_terminal_duplicates_total")
            return
        if task.get("cancel_requested") is True and task.get("state") != TaskState.SUCCESS.value:
            step = max([int(item.get("step", 0)) for item in task.get("trace", [])] or [0]) + 1
            self.store.cancel(
                task_id,
                TraceEvent(step, TaskState.CANCELLED, "Task was cancelled", utc_now()),
            )
            metrics.inc("reviews_cancelled_total")
            metrics.inc("review_admission_releases_total")
            return
        tenant_id = str(task.get("tenant_id") or "default")
        repository = str(task["repository"])
        pull_request = task.get("pull_request")
        expected = {
            "tenant_id": tenant_id,
            "repository": repository,
            "pull_request": pull_request,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise PermanentTaskError("queued task binding does not match persisted task")
        raw_generation = payload.get("admission_generation")
        execution_generation = (
            raw_generation
            if isinstance(raw_generation, int)
            and not isinstance(raw_generation, bool)
            and raw_generation >= 1
            else None
        )
        if task.get("state") != TaskState.SUCCESS.value:
            if execution_generation is None:
                raise PermanentTaskError("queued review admission is inactive or stale")
            if not self.store.review_admission_active(task_id, execution_generation):
                if self.store.review_admission_active(task_id):
                    metrics.inc("queue_terminal_duplicates_total")
                    return
                raise PermanentTaskError("queued review admission is inactive or stale")
        task_input = task.get("input") or {}
        if task_input.get("_delivery_complete") is True:
            metrics.inc("queue_terminal_duplicates_total")
            return
        trusted_payload = {**payload, **expected}
        for key in _QUEUE_CONTEXT_FIELDS:
            if key in task_input:
                if key in payload and payload[key] != task_input[key]:
                    raise PermanentTaskError("queued task binding does not match persisted task")
                trusted_payload[key] = task_input[key]
            else:
                trusted_payload.pop(key, None)
        installation_id = task_input.get("installation_id")
        if installation_id is not None and (
            not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or installation_id < 1
            or self.store.installation_tenant(installation_id) != tenant_id
        ):
            raise PermanentTaskError("task GitHub installation is not bound to persisted tenant")
        try:
            policy = self.policies.from_snapshot(task_input.get("repository_policy"))
        except ValueError:
            raise PermanentTaskError("task repository policy snapshot is invalid") from None
        if (
            task.get("state") != TaskState.SUCCESS.value
            and not self.policies.resolve(tenant_id, repository).enabled
        ):
            raise AccessDeniedError("repository was disabled after this task was accepted")
        if task.get("state") == TaskState.SUCCESS.value:
            report = self._persisted_report(task)
            self._deliver_review_result(
                trusted_payload,
                task_input,
                task_id,
                report,
                policy,
            )
            metrics.inc("queue_terminal_duplicates_total")
            return
        diff = self.store.get_task_payload(task_id)
        fetched_diff = False
        if diff is None and trusted_payload.get("diff_url"):
            client = self.code_host_for_installation(trusted_payload.get("installation_id"))
            client.ensure_repository_access(repository)
            diff = client.fetch_diff(
                trusted_payload["diff_url"], max_bytes=self.options.max_diff_bytes + 4096
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
                    pull_request,
                    diff,
                    tenant_id,
                    execution_generation,
                )
            self._run_shadow_safely(task_id, tenant_id, diff, report)
        except Exception:
            latest = self.store.get(task_id, tenant_id) or {}
            if latest.get("state") == TaskState.SUCCESS.value:
                self._deliver_review_result(
                    trusted_payload,
                    latest.get("input") or {},
                    task_id,
                    self._persisted_report(latest),
                    policy,
                )
                metrics.inc("queue_terminal_duplicates_total")
                return
            if latest.get("state") == TaskState.CANCELLED.value:
                metrics.inc("reviews_cancelled_total")
                metrics.inc("review_admission_releases_total")
                return
            metrics.inc("review_attempts_failed_total")
            raise
        self._review_succeeded(task_id, tenant_id)
        self._deliver_review_result(
            trusted_payload,
            task_input,
            task_id,
            report,
            policy,
        )

    @staticmethod
    def _persisted_report(task: dict[str, Any]) -> ReviewReport:
        raw_report = task.get("report")
        if not isinstance(raw_report, dict):
            raise PermanentTaskError("successful task report is invalid")
        try:
            return ReviewReport.from_dict(raw_report)
        except (KeyError, TypeError, ValueError):
            raise PermanentTaskError("successful task report is invalid") from None

    def _deliver_review_result(
        self,
        payload: dict[str, Any],
        task_input: dict[str, Any],
        task_id: str,
        report: ReviewReport,
        policy: RepositoryPolicy,
    ) -> None:
        if task_input.get("_session_recorded") is True:
            continuity = task_input.get("_continuity_note", "")
            continuity = continuity if isinstance(continuity, str) else ""
        else:
            continuity = self.record_session_turn(payload, report)
            self.store.update_task_input(
                task_id,
                {"_session_recorded": True, "_continuity_note": continuity},
            )
            task_input.update({"_session_recorded": True, "_continuity_note": continuity})
        if (
            payload.get("github_issue_url")
            and self.options.auto_post_review
            and policy.post_review_comments
            and self._comment_publishable(payload)
        ):
            self._post_review_comment(payload, task_id, report, continuity)
        self.store.update_task_input(
            task_id,
            {"_delivery_complete": True, "_delivery_resume_active": False},
        )
        task_input.update({"_delivery_complete": True, "_delivery_resume_active": False})

    def _session_publishable(self, payload: dict[str, Any]) -> bool:
        if not payload.get("session_id"):
            return True
        timeline = self.store.get_session_timeline(
            str(payload["session_id"]),
            str(payload.get("tenant_id") or "default"),
            1,
        )
        turns = timeline.get("turns") if timeline else None
        # ponytail: this latest-turn check covers completed overlap; add a provider-side
        # conditional update only if concurrent outbound comment writes are observed.
        return bool(
            timeline
            and timeline.get("status") not in {"closed", "draft"}
            and isinstance(turns, list)
            and turns
            and turns[-1].get("id") == payload.get("turn_id")
        )

    def _comment_publishable(self, payload: dict[str, Any]) -> bool:
        policy = self.policies.resolve(
            str(payload.get("tenant_id") or "default"), str(payload["repository"])
        )
        return bool(
            policy.enabled and policy.post_review_comments and self._session_publishable(payload)
        )

    def _post_review_comment(
        self,
        payload: dict[str, Any],
        task_id: str,
        report: Any,
        continuity: str,
    ) -> None:
        client = self.code_host_for_installation(payload.get("installation_id"))
        marker = (
            "<!-- evoagent-session:%s -->" % payload["session_id"]
            if payload.get("session_id")
            else "<!-- evoagent-review:%s -->" % task_id
        )
        suffix = "\n\n" + continuity if continuity else ""
        body = (
            to_markdown(
                report.to_dict(),
                max_chars=_COMMENT_MAX_CHARS - len(marker) - 1 - len(suffix),
            )
            + suffix
        )
        truncated = "> **Comment truncated**" in body
        effect_key = (
            "github-comment:"
            + hashlib.sha256(
                (payload["github_issue_url"] + "\n" + marker + "\n" + task_id).encode("utf-8")
            ).hexdigest()
        )
        owner = uuid.uuid4().hex
        receipt = self.store.claim_effect(
            effect_key,
            owner,
            self.options.effect_lease_seconds,
        )
        if receipt["status"] == "acquired":
            try:

                def authorize_write() -> None:
                    if not self.store.renew_effect(
                        effect_key, owner, self.options.effect_lease_seconds
                    ):
                        metrics.inc("effect_lease_conflicts_total")
                        raise RuntimeError("comment publication lease was lost before publication")
                    if not self._comment_publishable(payload):
                        raise _CommentSuppressed

                suppressed = False
                try:
                    client.upsert_comment(
                        payload["github_issue_url"], body, marker, before_write=authorize_write
                    )
                except _CommentSuppressed:
                    suppressed = True
                result: dict[str, Any] = {"marker": marker}
                if suppressed:
                    result["suppressed"] = True
                if not self.store.complete_effect(effect_key, owner, result):
                    metrics.inc("effect_lease_conflicts_total")
                    raise RuntimeError("external effect lease expired before completion")
                if truncated and not suppressed:
                    metrics.inc("github_comment_truncations_total")
            except Exception as exc:
                self.store.release_effect(
                    effect_key,
                    owner,
                    safe_exception_summary(exc, "external effect failed"),
                )
                raise
        elif receipt["status"] == "busy":
            metrics.inc("effect_receipt_busy_total")
            raise RuntimeError("external effect is already in progress")
        elif receipt["status"] != "completed":
            raise RuntimeError("external effect receipt is invalid")

    def _review_succeeded(self, task_id: str, tenant_id: str) -> None:
        metrics.inc("reviews_total")
        metrics.inc("review_admission_releases_total")
        self._observe_release(tenant_id, task_id, False)
        self._evaluate_alerts(tenant_id)

    def _observe_release(self, tenant_id: str, task_id: str, failed: bool) -> None:
        try:
            task = self.store.get(task_id, tenant_id) or {}
            task_input = task.get("input") or {}
            self.releases.observe(
                tenant_id,
                "llm-review",
                task_id,
                failed,
                task_input.get("release_lane", "stable"),
                task_input.get("release_candidate_version"),
                task_input.get("release_generation"),
            )
        except Exception:
            # ponytail: preserve the durable review outcome; add an outcome outbox
            # only when governance observations must be replayed automatically.
            metrics.inc("release_observation_failures_total")

    def _evaluate_alerts(self, tenant_id: str) -> None:
        try:
            self.alerts.evaluate(tenant_id)
        except Exception:
            metrics.inc("alert_evaluation_failures_total")

    def _review_failed(
        self,
        task_id: str,
        tenant_id: str,
        mode: str,
    ) -> None:
        metrics.inc("reviews_failed_total")
        if mode == "synchronous":
            metrics.inc("review_admission_releases_total")
        self._observe_release(tenant_id, task_id, True)
        self._evaluate_alerts(tenant_id)

    def on_dead_letter(self, payload: dict[str, Any], error: str) -> None:
        error = coerce_safe_summary(error, "task delivery failed")
        task_id = payload.get("task_id", "")
        task = self.store.get(task_id) if task_id else None
        tenant_id = str(task.get("tenant_id") or "system") if task else "system"
        raw_generation = payload.get("admission_generation")
        generation = (
            raw_generation
            if isinstance(raw_generation, int)
            and not isinstance(raw_generation, bool)
            and raw_generation >= 1
            else -1
        )
        cancelled = bool(
            task
            and (
                task.get("state") == TaskState.CANCELLED.value
                or (
                    task.get("cancel_requested") is True
                    and task.get("state") != TaskState.SUCCESS.value
                )
            )
        )
        terminal_failure = bool(task and task.get("state") == TaskState.FAILED.value)
        if (
            generation != -1
            and task
            and task.get("state")
            not in {
                TaskState.SUCCESS.value,
                TaskState.CANCELLED.value,
            }
        ):
            step = max([int(item.get("step", 0)) for item in task.get("trace", [])] or [0]) + 1
            if cancelled:
                if self.store.cancel(
                    task_id,
                    TraceEvent(step, TaskState.CANCELLED, "Task was cancelled", utc_now()),
                    generation,
                ):
                    metrics.inc("review_admission_releases_total")
            elif task.get("state") != TaskState.FAILED.value:
                terminal_failure = bool(
                    self.store.fail(
                        task_id,
                        error,
                        TraceEvent(
                            step,
                            TaskState.FAILED,
                            error,
                            utc_now(),
                        ),
                        generation,
                    )
                )
        admission_released = bool(
            task_id and self.store.release_review_admission(task_id, "dead-letter", generation)
        )
        if admission_released:
            metrics.inc("review_admission_releases_total")
            if terminal_failure:
                self._review_failed(task_id, tenant_id, "asynchronous")
        if (
            task_id
            and task
            and task.get("state") == TaskState.SUCCESS.value
            and payload.get("delivery_only") is True
        ):
            message_id = payload.get("_queue_message_id")
            if isinstance(message_id, str) and message_id:
                self.store.release_review_delivery_resume(task_id, message_id)
        if cancelled:
            metrics.inc("reviews_cancelled_total")
            metrics.inc("dead_letters_total")
            return
        self.store.create_alert(
            tenant_id,
            "dlq:%s" % (task_id or "unknown"),
            "critical",
            error,
        )
        metrics.inc("dead_letters_total")

    def record_feedback(
        self,
        task_id: str,
        category: str,
        finding: dict[str, Any] | None,
        note: str,
        tenant_id: str | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        if category not in {"false_positive", "missed_issue", "bad_fix", "accepted"}:
            raise ClientInputError("unsupported feedback category")
        if not self.store.record_failure_case(
            task_id,
            category,
            {"finding": finding, "note": note[:2000]},
            tenant_id=tenant_id,
            actor=actor,
        ):
            raise ResourceNotFoundError("task not found")
        metrics.inc("feedback_total")
        metrics.inc("feedback_%s_total" % category)
        return {"recorded": True, "category": category}

    def resume_task(self, task_id: str, tenant_id: str, actor: str = "system") -> dict[str, Any]:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ResourceNotFoundError("task not found")
        task_input = task.get("input") or {}
        actual_tenant = str(task.get("tenant_id") or tenant_id)
        if task["state"] == "SUCCESS" and (
            task_input.get("_delivery_complete") is True
            or not (task_input.get("session_id") or task_input.get("github_issue_url"))
        ):
            self.store.audit(actual_tenant, actor, "task.resume", task_id, {"status": "complete"})
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if task["state"] == TaskState.CANCELLED.value:
            raise StateConflictError("cancelled task cannot be resumed")
        resume_id = uuid.uuid4().hex
        payload = {
            "task_id": task_id,
            "repository": task["repository"],
            "pull_request": task.get("pull_request"),
            "tenant_id": actual_tenant,
        }
        if task["state"] == "SUCCESS":
            payload["delivery_only"] = True
            result = self.store.resume_review_delivery(
                task_id,
                actual_tenant,
                "review-delivery:" + resume_id,
                "review-delivery:" + resume_id,
                payload,
                actor,
            )
            if result["status"] == "resumed":
                metrics.inc("review_delivery_resumes_total")
                self.notify_outbox()
            if result["status"] == "missing":
                raise ResourceNotFoundError("task not found")
            if result["status"] not in {"resumed", "active", "complete"}:
                raise StateConflictError("task delivery cannot be resumed")
            return {
                "task_id": task_id,
                "state": "SUCCESS",
                "report": task["report"],
                "delivery_resumed": result["status"] == "resumed",
                "delivery_already_active": result["status"] == "active",
                "delivery_complete": result["status"] == "complete",
            }
        if self.store.get_task_payload(task_id) is None and not task_input.get("diff_url"):
            raise StateConflictError("task payload is no longer available")
        try:
            result = self.store.resume_review_task(
                task_id,
                actual_tenant,
                self.options.tenant_max_active_reviews,
                "review-resume:" + resume_id,
                "review-resume:" + resume_id,
                payload,
                actor,
            )
        except TenantReviewCapacityError:
            metrics.inc("review_admission_rejections_total")
            raise
        status = result["status"]
        if status == "missing":
            raise ResourceNotFoundError("task not found")
        if status == "cancelled":
            raise StateConflictError("cancelled task cannot be resumed")
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

    def cancel_task(self, task_id: str, tenant_id: str, actor: str = "system") -> dict[str, Any]:
        accepted = self.store.request_cancel(task_id, tenant_id, actor)
        task = self.store.get(task_id, tenant_id) if accepted else None
        state = str(task.get("state")) if task else None
        cancel_requested = bool(
            task and (task.get("cancel_requested") is True or state == TaskState.CANCELLED.value)
        )
        return {
            "accepted": bool(task),
            "cancel_requested": cancel_requested,
            "state": state,
        }
