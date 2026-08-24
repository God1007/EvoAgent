"""Proof execution and deterministic repair publication use cases."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from ..errors import (
    AccessDeniedError,
    ClientInputError,
    ResourceNotFoundError,
    StateConflictError,
    safe_exception_summary,
)
from ..metrics import metrics
from ..policy import RepositoryPolicyResolver
from ..ports import (
    CodeHostPort,
    ProofExecutorPort,
    RepairApplicationStorePort,
    RepairPublisherPort,
)
from ..proof import LocalProofExecutor, ProofRunner
from ..verifier import RepairVerifier


@dataclass(frozen=True)
class RepairOptions:
    max_diff_bytes: int
    verify_timeout_seconds: int
    container_image: str
    memory_mb: int
    pids_limit: int
    cpus: float
    max_output_bytes: int
    effect_lease_seconds: float


def _repair_audit_detail(result: dict[str, Any], replayed: bool) -> dict[str, Any]:
    detail = {"branch": result.get("branch"), "replayed": replayed}
    verification = result.get("verification")
    if isinstance(verification, dict):
        detail["verification_passed"] = verification.get("passed") is True
        if isinstance(verification.get("attestation"), dict):
            detail["attestation"] = verification["attestation"]
    return detail


class RepairUseCases:
    def __init__(
        self,
        store: RepairApplicationStorePort,
        policies: RepositoryPolicyResolver,
        fixer: RepairPublisherPort,
        code_host_for_installation: Callable[[int | None], CodeHostPort],
        options: RepairOptions,
        proof_executor: ProofExecutorPort | None = None,
    ):
        self.store = store
        self.policies = policies
        self.fixer = fixer
        self.code_host_for_installation = code_host_for_installation
        self.options = options
        self.proof_executor = proof_executor or LocalProofExecutor(self._proof_verifier)

    def _proof_verifier(self, command: str) -> RepairVerifier:
        # Proof commands and PR source are both untrusted. Host fallback is
        # never permitted for this use case, regardless of repair configuration.
        return RepairVerifier(
            command,
            self.options.verify_timeout_seconds,
            container_image=self.options.container_image,
            memory_mb=self.options.memory_mb,
            pids_limit=self.options.pids_limit,
            cpus=self.options.cpus,
            require_container=True,
            max_output_bytes=self.options.max_output_bytes,
        )

    def run_proof(
        self,
        original_files: dict[str, Any],
        patched_files: dict[str, Any],
        reproduction_command: str = "",
        regression_command: str = "",
    ) -> dict[str, Any]:
        if not isinstance(original_files, dict) or not isinstance(patched_files, dict):
            raise ClientInputError("'original' and 'patched' file maps are required")
        if any(
            not isinstance(path, str) or not isinstance(text, str)
            for files in (original_files, patched_files)
            for path, text in files.items()
        ):
            raise ClientInputError("proof files must map string paths to string contents")
        try:
            total = sum(
                len(path.encode("utf-8")) + len(text.encode("utf-8"))
                for files in (original_files, patched_files)
                for path, text in files.items()
            )
        except UnicodeEncodeError:
            raise ClientInputError("proof paths and contents must be valid UTF-8") from None
        if total > self.options.max_diff_bytes * 10:
            raise ClientInputError("proof payload exceeds the maximum analysable size")
        with metrics.latency("proof_execution"):
            result = ProofRunner(executor=self.proof_executor).prove(
                cast(dict[str, str], original_files),
                cast(dict[str, str], patched_files),
                str(reproduction_command or ""),
                str(regression_command or ""),
            )
        metrics.inc("proof_runs_total")
        metrics.inc("proof_evidence_l%d_total" % int(result["evidence_level"]))
        if any(step.get("status") in {"error", "timeout"} for step in result["steps"]):
            metrics.inc("proof_inconclusive_total")
        return result

    def create_fix(
        self,
        task_id: str,
        tenant_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ResourceNotFoundError("completed task not found")
        if not task.get("report"):
            raise StateConflictError("task review is not complete")
        if task.get("pull_request") is None:
            raise StateConflictError("fix commits require a GitHub pull request task")
        task_input = task.get("input") or {}
        head_sha = task_input.get("head_sha")
        if not isinstance(head_sha, str) or not head_sha:
            raise StateConflictError("fix commits require a review with a snapshotted PR head")
        installation_id = task_input.get("installation_id")
        actual_tenant = task.get("tenant_id") or tenant_id or "default"
        if installation_id is not None and (
            not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or installation_id < 1
            or self.store.installation_tenant(installation_id) != actual_tenant
        ):
            raise AccessDeniedError("GitHub installation is not bound to task tenant")
        policy = self.policies.resolve(actual_tenant, task["repository"])
        available_rule_ids = tuple(getattr(self.fixer, "rule_ids", ()))
        allowed_rule_ids = self.policies.authorize_fix(policy, available_rule_ids)
        effect_key = "fix-pr:%s:%s" % (actual_tenant, task_id)
        owner = uuid.uuid4().hex
        receipt = self.store.claim_effect(
            effect_key,
            owner,
            self.options.effect_lease_seconds,
        )
        if receipt["status"] == "completed":
            metrics.inc("fix_idempotent_replays_total")
            result = dict(receipt.get("result") or {})
            self.store.audit(
                actual_tenant,
                actor or "system",
                "repair.create",
                task_id,
                _repair_audit_detail(result, True),
            )
            return {**result, "replayed": True}
        if receipt["status"] == "busy":
            raise StateConflictError("a repair publication for this task is already in progress")
        metrics.inc("fix_attempts_total")

        def authorize_publication(applied_rule_ids: tuple[str, ...]) -> None:
            current = self.policies.resolve(actual_tenant, task["repository"])
            currently_allowed = self.policies.authorize_fix(current, available_rule_ids)
            if not set(applied_rule_ids).issubset(currently_allowed):
                raise AccessDeniedError(
                    "automatic repair rules are no longer enabled by repository policy"
                )
            if not self.store.renew_effect(effect_key, owner, self.options.effect_lease_seconds):
                metrics.inc("effect_lease_conflicts_total")
                raise RuntimeError("repair publication lease was lost before publication")

        try:
            result = self.fixer.create_fix_commits(
                self.code_host_for_installation(installation_id),
                task["repository"],
                task["pull_request"],
                task["report"],
                operation_key=effect_key,
                allowed_rule_ids=allowed_rule_ids,
                expected_source_sha=head_sha,
                before_publish=authorize_publication,
            )
            if not self.store.complete_effect(
                effect_key,
                owner,
                result,
                audit_event=(
                    actual_tenant,
                    actor or "system",
                    "repair.create",
                    task_id,
                    _repair_audit_detail(result, False),
                ),
            ):
                metrics.inc("effect_lease_conflicts_total")
                raise RuntimeError("repair publication lease was lost before completion")
        except Exception as exc:
            metrics.inc("fix_failed_total")
            self.store.release_effect(
                effect_key,
                owner,
                safe_exception_summary(exc, "external effect failed"),
            )
            raise
        metrics.inc("fix_runs_total")
        commits = len(result.get("commits", []))
        if result.get("branch"):
            metrics.inc("fix_published_total")
            metrics.inc("fix_commits_total", commits)
        elif "verification" in result:
            metrics.inc("fix_verification_blocked_total")
        else:
            metrics.inc("fix_abstained_total")
        return {**result, "replayed": False}
