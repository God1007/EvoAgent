"""GitHub webhook admission and durable review creation use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..errors import ClientInputError
from ..metrics import metrics
from ..ports import TaskQueuePort, WebhookApplicationStorePort
from .reviews import ReviewUseCases


@dataclass(frozen=True)
class WebhookOptions:
    default_tenant_id: str
    auto_post_review: bool


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
        action = payload.get("action")
        existing = self.store.get_webhook(delivery_id)
        if existing is not None:
            duplicate = self._duplicate(existing, payload_sha256)
            if existing.get("task_id") or action not in {"opened", "reopened", "synchronize"}:
                return duplicate

        installation_id = (payload.get("installation") or {}).get("id")
        tenant_id = (
            tenant_id
            or (self.store.installation_tenant(installation_id) if installation_id else None)
            or self.options.default_tenant_id
        )
        if action not in {"opened", "reopened", "synchronize"}:
            if not self.store.claim_webhook(delivery_id, tenant_id, "pull_request", payload_sha256):
                return self._duplicate(self.store.get_webhook(delivery_id) or {}, payload_sha256)
            self.store.complete_webhook(delivery_id, None)
            return {"ignored": True, "reason": "unsupported pull_request action: %s" % action}

        pull = payload.get("pull_request") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        number = payload.get("number")
        diff_url = pull.get("diff_url")
        if not repository or not isinstance(number, int) or not diff_url:
            raise ClientInputError("invalid GitHub pull_request payload")
        policy = self.reviews.authorize_repository(tenant_id, repository)
        head_sha = (pull.get("head") or {}).get("sha")
        task_id, task_input = self.reviews.prepare_deferred_task(
            repository,
            "github-webhook",
            tenant_id,
            {"diff_url": diff_url, "trigger": action},
            policy,
        )
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
                "github_issue_url": pull.get("issue_url", ""),
                "installation_id": installation_id,
                "tenant_id": tenant_id,
                "diff_url": diff_url,
            },
        )
        if not accepted["accepted"]:
            current = self.store.get_webhook(delivery_id) or {
                "payload_sha256": payload_sha256,
                "task_id": accepted.get("task_id"),
            }
            return self._duplicate(current, payload_sha256)

        self.notify_outbox()
        metrics.inc("reviews_enqueued_total")
        return {
            "task_id": task_id,
            "state": "PENDING",
            "queue": self.queue().backend,
            "session_id": accepted["session_id"],
            "turn": accepted["sequence"],
            "will_post_to_github": (self.options.auto_post_review and policy.post_review_comments),
        }
