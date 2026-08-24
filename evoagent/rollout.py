"""Deterministic canary and shadow assignment with automatic rollback."""

import hashlib
import math

from .errors import ClientInputError
from .metrics import metrics
from .ports import ReleaseStorePort


def _percentage(config: dict[str, object], key: str) -> int:
    value = config.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ClientInputError("%s must be an integer" % key)
    return value


def _rate(config: dict[str, object], key: str, default: float) -> float:
    value = config.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ClientInputError("%s must be a finite number between 0 and 1" % key)
    return float(value)


def _finding_keys(payload: dict[str, object] | None) -> set[str]:
    values = (payload or {}).get("finding_keys", [])
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(value) for value in values}


def _valid_release_identity(candidate_version: int | None, generation: int | None) -> bool:
    return all(
        type(value) is int and 1 <= value <= 2**31 - 1 for value in (candidate_version, generation)
    )


class ReleaseManager:
    CONFIG_FIELDS = frozenset(
        {
            "stable_version",
            "candidate_version",
            "canary_percent",
            "shadow_percent",
            "max_error_rate",
            "min_samples",
            "max_disagreement_rate",
            "auto_promote",
        }
    )

    def __init__(self, store: ReleaseStorePort, execution_revision: str = ""):
        if not isinstance(execution_revision, str) or (
            execution_revision
            and (
                len(execution_revision) != 64
                or any(character not in "0123456789abcdef" for character in execution_revision)
            )
        ):
            raise ValueError("release execution revision must be a SHA-256 digest")
        self.store = store
        self.execution_revision = execution_revision

    def configure(
        self,
        tenant_id: str,
        skill_name: str,
        config: dict[str, object],
        actor: str = "system",
    ) -> dict:
        unknown = set(config).difference(self.CONFIG_FIELDS)
        if unknown:
            raise ClientInputError("unsupported deployment fields: %s" % ", ".join(sorted(unknown)))
        canary = _percentage(config, "canary_percent")
        shadow = _percentage(config, "shadow_percent")
        if not 0 <= canary <= 100 or not 0 <= shadow <= 100:
            raise ClientInputError("canary_percent and shadow_percent must be between 0 and 100")
        candidate = config.get("candidate_version")
        stable = config.get("stable_version")
        if (
            isinstance(candidate, bool)
            or not isinstance(candidate, int)
            or not 1 <= candidate <= 2**31 - 1
        ):
            raise ClientInputError("candidate_version must be a positive integer")
        if stable is not None and (
            isinstance(stable, bool) or not isinstance(stable, int) or not 1 <= stable <= 2**31 - 1
        ):
            raise ClientInputError("stable_version must be a positive integer or null")
        if stable == candidate:
            raise ClientInputError("stable_version and candidate_version must differ")
        min_samples = config.get("min_samples", 20)
        if (
            isinstance(min_samples, bool)
            or not isinstance(min_samples, int)
            or not 1 <= min_samples <= 2**31 - 1
        ):
            raise ClientInputError("min_samples must be a positive integer")
        auto_promote = config.get("auto_promote", False)
        if not isinstance(auto_promote, bool):
            raise ClientInputError("auto_promote must be a boolean")
        if auto_promote and shadow == 0:
            raise ClientInputError("auto_promote requires shadow_percent greater than zero")
        candidate_version = self.store.get_skill_version(skill_name, candidate)
        if candidate_version is None:
            raise ClientInputError("candidate_version does not exist")
        if candidate_version.get("qualification") != "approved":
            raise ClientInputError("candidate_version is not approved for rollout")
        if self.execution_revision and (
            self.store.get_skill_evaluation_revision(skill_name, candidate)
            != self.execution_revision
        ):
            raise ClientInputError(
                "candidate_version must be re-evaluated under the current execution revision"
            )
        stable_version = (
            self.store.get_skill_version(skill_name, stable) if stable is not None else None
        )
        if stable is not None and stable_version is None:
            raise ClientInputError("stable_version does not exist")
        if stable_version is not None and stable_version.get("qualification") not in {
            "approved",
            "legacy",
        }:
            raise ClientInputError("stable_version is not eligible for rollout")
        normalized = {
            "stable_version": stable,
            "candidate_version": candidate,
            "canary_percent": canary,
            "shadow_percent": shadow,
            "max_error_rate": _rate(config, "max_error_rate", 0.1),
            "min_samples": min_samples,
            "max_disagreement_rate": _rate(config, "max_disagreement_rate", 0.2),
            "auto_promote": auto_promote,
            "status": "running",
        }
        return self.store.save_deployment(tenant_id, skill_name, normalized, actor)

    def assignment(self, tenant_id: str, skill_name: str, key: str) -> dict[str, object]:
        deployment = self.store.get_deployment(tenant_id, skill_name)
        if not deployment or deployment["status"] != "running":
            return {"lane": "stable", "shadow": False, "deployment": deployment}
        if self.execution_revision and (
            self.store.get_skill_evaluation_revision(skill_name, deployment["candidate_version"])
            != self.execution_revision
        ):
            metrics.inc("release_revision_mismatch_total")
            return {
                "lane": "stable",
                "shadow": False,
                "deployment": {
                    **deployment,
                    "candidate_version": None,
                    "generation": None,
                },
            }
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
        task_id: str,
        failed: bool,
        lane: str = "canary",
        candidate_version: int | None = None,
        generation: int | None = None,
    ) -> dict | None:
        if lane != "canary":
            return None
        if (
            not isinstance(task_id, str)
            or not task_id
            or not _valid_release_identity(candidate_version, generation)
        ):
            return None
        assert candidate_version is not None and generation is not None
        result = self.store.record_deployment_result(
            tenant_id, skill_name, task_id, failed, candidate_version, generation
        )
        if result and result["status"] == "rolled_back":
            try:
                self.store.create_alert(
                    tenant_id,
                    "rollout:%s" % skill_name,
                    "critical",
                    "Canary %s was automatically rolled back after exceeding its error budget."
                    % skill_name,
                )
            except Exception:
                metrics.inc("release_alert_failures_total")
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
        candidate_version: int | None = None,
        generation: int | None = None,
        audit_event: tuple[str, dict[str, object]] | None = None,
    ) -> dict | None:
        if lane not in {"stable", "canary"} or not _valid_release_identity(
            candidate_version, generation
        ):
            return None
        assert candidate_version is not None and generation is not None
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
            candidate_version,
            generation,
            candidate_failed=candidate_failed,
            audit_event=audit_event,
        )
        if result and result["status"] == "promoted":
            try:
                self.store.create_alert(
                    tenant_id,
                    "rollout-promoted:%s" % skill_name,
                    "info",
                    "Candidate %s was automatically promoted after shadow verification."
                    % skill_name,
                )
            except Exception:
                metrics.inc("release_alert_failures_total")
        return result
