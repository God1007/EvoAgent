"""Administrative model-usage reconciliation use cases."""

from __future__ import annotations

from typing import Any

from ..errors import ClientInputError
from ..metrics import metrics
from ..ports import ModelUsageStorePort


class ModelUsageUseCases:
    def __init__(self, store: ModelUsageStorePort):
        self.store = store

    def reconcile(
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
        if not tenant_id or not actor:
            raise ClientInputError("model usage reconciliation requires tenant and actor")
        if not request_id or len(request_id) > 128:
            raise ClientInputError("model usage request_id must contain 1 to 128 characters")
        if status not in {"success", "failed"}:
            raise ClientInputError("model usage status must be success or failed")
        values = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_micros": cost_micros,
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ClientInputError("%s must be a non-negative integer" % name)
        if not isinstance(error, str) or len(error) > 2000:
            raise ClientInputError(
                "model usage reconciliation error must be at most 2000 characters"
            )
        reconciled = self.store.reconcile_model_usage(
            tenant_id,
            actor,
            request_id,
            status,
            input_tokens,
            output_tokens,
            cost_micros,
            error,
        )
        if not reconciled:
            return {"request_id": request_id, "reconciled": False}
        detail = {
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_micros": cost_micros,
        }
        metrics.inc("model_reservations_reconciled_total")
        return {"request_id": request_id, "reconciled": True, **detail}
