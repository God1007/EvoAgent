"""Deterministic canary and shadow assignment with automatic rollback."""

import hashlib

from .errors import ClientInputError
from .ports import ReleaseStorePort


def _percentage(config: dict[str, object], key: str) -> int:
    value = config.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ClientInputError("%s must be an integer" % key)
    try:
        return int(value)
    except ValueError:
        raise ClientInputError("%s must be an integer" % key) from None


def _finding_keys(payload: dict[str, object] | None) -> set[str]:
    values = (payload or {}).get("finding_keys", [])
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(value) for value in values}


class ReleaseManager:
    def __init__(self, store: ReleaseStorePort):
        self.store = store

    def configure(self, tenant_id: str, skill_name: str, config: dict[str, object]) -> dict:
        canary = _percentage(config, "canary_percent")
        shadow = _percentage(config, "shadow_percent")
        if not 0 <= canary <= 100 or not 0 <= shadow <= 100:
            raise ClientInputError("canary_percent and shadow_percent must be between 0 and 100")
        if config.get("candidate_version") is None:
            raise ClientInputError("candidate_version is required")
        self.store.save_deployment(tenant_id, skill_name, config)
        deployment = self.store.get_deployment(tenant_id, skill_name)
        if deployment is None:
            raise RuntimeError("deployment was not persisted")
        return deployment

    def assignment(self, tenant_id: str, skill_name: str, key: str) -> dict[str, object]:
        deployment = self.store.get_deployment(tenant_id, skill_name)
        if not deployment or deployment["status"] != "running":
            return {"lane": "stable", "shadow": False, "deployment": None}
        bucket = (
            int(
                hashlib.sha256(
                    ("%s:%s:%s" % (tenant_id, skill_name, key)).encode("utf-8")
                ).hexdigest()[:8],
                16,
            )
            % 100
        )
        return {
            "lane": "canary" if bucket < deployment["canary_percent"] else "stable",
            "shadow": bucket < deployment["shadow_percent"],
            "deployment": deployment,
        }

    def observe(
        self,
        tenant_id: str,
        skill_name: str,
        failed: bool,
        lane: str = "canary",
    ) -> dict | None:
        if lane != "canary":
            return self.store.get_deployment(tenant_id, skill_name)
        result = self.store.record_deployment_result(tenant_id, skill_name, failed)
        if result and result["status"] == "rolled_back":
            self.store.create_alert(
                tenant_id,
                "rollout:%s" % skill_name,
                "critical",
                "Canary %s was automatically rolled back after exceeding its error budget."
                % skill_name,
            )
        return result

    def observe_shadow(
        self,
        tenant_id: str,
        skill_name: str,
        task_id: str,
        lane: str,
        primary: dict[str, object],
        candidate: dict[str, object] | None,
        candidate_failed: bool = False,
    ) -> dict | None:
        primary_keys = _finding_keys(primary)
        candidate_keys = _finding_keys(candidate)
        union = primary_keys | candidate_keys
        disagreement = len(primary_keys ^ candidate_keys) / len(union) if union else 0.0
        result = self.store.record_shadow_observation(
            tenant_id,
            skill_name,
            task_id,
            lane,
            primary,
            candidate,
            disagreement,
            candidate_failed,
        )
        if result and result["status"] == "promoted":
            self.store.create_alert(
                tenant_id,
                "rollout-promoted:%s" % skill_name,
                "info",
                "Candidate %s was automatically promoted after shadow verification." % skill_name,
            )
        return result
