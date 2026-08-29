"""GitHub webhook admission and durable review creation use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..errors import AccessDeniedError, ClientInputError, TenantReviewCapacityError
from ..github import GITHUB_INSTALLATION_ID_MAX, validate_commit_sha, validate_pull_request_urls
from ..metrics import metrics
from ..models import ReviewContext
from ..ports import TaskQueuePort, WebhookApplicationStorePort
from ..repository import canonical_repository
from .reviews import ReviewUseCases

REVIEW_ACTIONS = frozenset({"opened", "reopened", "synchronize", "ready_for_review"})
END_ACTIONS = {"closed": "closed", "converted_to_draft": "draft"}


@dataclass(frozen=True)
class WebhookOptions:
    default_tenant_id: str
    auto_post_review: bool
    require_installation_binding: bool = False


def github_pull_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    pull = payload.get("pull_request")
    if not isinstance(pull, dict):
        raise ClientInputError("invalid GitHub pull_request payload")
    return pull


def github_pull_request_updated_at(payload: dict[str, Any]) -> datetime:
    value = github_pull_request_payload(payload).get("updated_at")
    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00") if isinstance(value, str) and len(value) <= 64 else ""
        )
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError:
        raise ClientInputError("invalid pull_request.updated_at") from None


class WebhookUseCases:
    def __init__(
        self,
        store: WebhookApplicationStorePort,
        reviews: ReviewUseCases,
        queue: Callable[[], TaskQueuePort],
        notify_outbox: Callable[[], None],
        options: WebhookOptions,
    ):
        self.store = store
        self.reviews = reviews
        self.queue = queue
        self.notify_outbox = notify_outbox
        self.options = options

    @staticmethod
    def _duplicate(existing: dict[str, Any], payload_sha256: str) -> dict[str, Any]:
        if existing.get("payload_sha256") != payload_sha256:
            raise ClientInputError("delivery id was already used with a different payload")
        task_id = existing.get("task_id")
        return {
            "duplicate": True,
            "task_id": task_id,
            "state": "PENDING" if task_id else "ACCEPTED",
        }

    def handle_github_pull_request(
        self,
        payload: dict[str, Any],
        delivery_id: str,
        payload_sha256: str,
        tenant_id: str = "",
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ClientInputError("invalid GitHub webhook payload")
        if not isinstance(delivery_id, str) or not 1 <= len(delivery_id) <= 200:
            raise ClientInputError("invalid GitHub delivery id")
        if not isinstance(payload_sha256, str) or not 1 <= len(payload_sha256) <= 128:
            raise ClientInputError("invalid GitHub payload digest")
        if not isinstance(tenant_id, str) or len(tenant_id) > 200:
            raise ClientInputError("invalid GitHub webhook tenant")
        action = payload.get("action")
        if not isinstance(action, str) or not action or len(action) > 64:
            raise ClientInputError("invalid GitHub pull_request action")
        existing = self.store.get_webhook(delivery_id)
        if existing is not None:
            duplicate = self._duplicate(existing, payload_sha256)
            if existing.get("task_id") or action not in REVIEW_ACTIONS:
                return duplicate

        installation = payload.get("installation")
        if installation is not None and not isinstance(installation, dict):
            raise ClientInputError("invalid GitHub installation")
        installation_id = (installation or {}).get("id")
        if self.options.require_installation_binding and installation_id is None:
            raise AccessDeniedError("GitHub App webhook requires a bound installation")
        if installation_id is not None and (
            not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or installation_id <= 0
            or installation_id > GITHUB_INSTALLATION_ID_MAX
        ):
            raise ClientInputError("invalid GitHub installation id")
        bound_tenant = self.store.installation_tenant(installation_id) if installation_id else None
        if installation_id and not bound_tenant:
            raise AccessDeniedError("GitHub installation is not bound to a tenant")
        if tenant_id and bound_tenant and tenant_id != bound_tenant:
            raise AccessDeniedError("GitHub installation is bound to another tenant")
        tenant_id = tenant_id or bound_tenant or self.options.default_tenant_id
        if action not in REVIEW_ACTIONS and action not in END_ACTIONS:
            if not self.store.claim_webhook(delivery_id, tenant_id, "pull_request", payload_sha256):
                return self._duplicate(self.store.get_webhook(delivery_id) or {}, payload_sha256)
            self.store.complete_webhook(delivery_id, None)
            return {"ignored": True, "reason": "unsupported pull_request action: %s" % action}

        pull = github_pull_request_payload(payload)
        event_at = github_pull_request_updated_at(payload)
        draft = pull.get("draft", False)
        if not isinstance(draft, bool):
            raise ClientInputError("invalid GitHub pull_request draft state")
        repository_payload = payload.get("repository")
        if not isinstance(repository_payload, dict):
            raise ClientInputError("invalid GitHub pull_request payload")
        repository = canonical_repository(repository_payload.get("full_name", ""))
        number = payload.get("number")
        if number is None:
            raise ClientInputError("invalid GitHub pull_request payload")
        self.reviews.validate_pull_request(number)
        ended_status = END_ACTIONS.get(action) or ("draft" if draft else "")
        if ended_status:
            ended = self.store.finish_pull_request_webhook(
                delivery_id,
                tenant_id,
                payload_sha256,
                repository,
                number,
                ended_status,
                event_at,
            )
            if ended.get("stale"):
                metrics.inc("github_webhook_stale_events_total")
                return {"ignored": True, "reason": "stale pull_request event"}
            if not ended["accepted"]:
                return self._duplicate(self.store.get_webhook(delivery_id) or {}, payload_sha256)
            metrics.inc("reviews_cancelled_total", ended["cancelled"])
            metrics.inc("review_admission_releases_total", ended["released"])
            return {
                "ignored": True,
                "reason": (
                    "draft pull request" if ended_status == "draft" else "closed pull request"
                ),
                "cancelled_tasks": ended["cancelled"],
                "cancellation_requested": ended["cancel_requested"],
            }

        head = pull.get("head")
        base = pull.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ClientInputError("invalid GitHub pull_request payload")
        diff_url = pull.get("diff_url")
        issue_url = pull.get("issue_url")
        if (
            not isinstance(diff_url, str)
            or not diff_url
            or len(diff_url) > 4096
            or not isinstance(issue_url, str)
            or not issue_url
            or len(issue_url) > 4096
        ):
            raise ClientInputError("invalid GitHub pull_request payload")
        try:
            base_sha = validate_commit_sha(base.get("sha"))
            head_sha = validate_commit_sha(head.get("sha"))
            validate_pull_request_urls(repository, number, diff_url, issue_url)
        except ValueError:
            raise ClientInputError("invalid GitHub pull_request comparison") from None
        try:
            review_context = ReviewContext.from_github(
                pull.get("title"), pull.get("body")
            ).to_dict()
        except ValueError:
            raise ClientInputError("invalid GitHub pull_request title or body") from None
        policy = self.reviews.authorize_repository(tenant_id, repository)
        task_id, task_input = self.reviews.prepare_deferred_task(
            repository,
            "github-webhook",
            tenant_id,
            {
                "diff_url": diff_url,
                "base_sha": base_sha,
                "trigger": action,
                "installation_id": installation_id,
                "review_context": review_context,
            },
            policy,
        )
        try:
            accepted = self.store.accept_pull_request_webhook(
                delivery_id,
                tenant_id,
                payload_sha256,
                repository,
                number,
                head_sha,
                action,
                task_id,
                task_input,
                {
                    "repository": repository,
                    "pull_request": number,
                    "github_issue_url": issue_url,
                    "installation_id": installation_id,
                    "tenant_id": tenant_id,
                    "diff_url": diff_url,
                    "base_sha": base_sha,
                },
                self.reviews.options.tenant_max_active_reviews,
                event_at=event_at,
            )
        except TenantReviewCapacityError:
            metrics.inc("review_admission_rejections_total")
            raise
        if accepted.get("stale"):
            metrics.inc("github_webhook_stale_events_total")
            return {"ignored": True, "reason": "stale pull_request event"}
        if not accepted["accepted"]:
            current = self.store.get_webhook(delivery_id) or {
                "payload_sha256": payload_sha256,
                "task_id": accepted.get("task_id"),
            }
            return self._duplicate(current, payload_sha256)

        self.notify_outbox()
        metrics.inc("review_admissions_total")
        metrics.inc("reviews_enqueued_total")
        return {
            "task_id": task_id,
            "state": "PENDING",
            "queue": self.queue().backend,
            "session_id": accepted["session_id"],
            "turn": accepted["sequence"],
            "will_post_to_github": (self.options.auto_post_review and policy.post_review_comments),
        }
