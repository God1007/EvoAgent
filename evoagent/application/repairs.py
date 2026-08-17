"""Proof execution and deterministic repair publication use cases."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..metrics import metrics
from ..policy import RepositoryPolicyResolver
from ..ports import CodeHostPort, RepairApplicationStorePort, RepairPublisherPort
from ..proof import ProofRunner
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


class RepairUseCases:
    def __init__(
        self,
        store: RepairApplicationStorePort,
        policies: RepositoryPolicyResolver,
        fixer: RepairPublisherPort,
        code_host_for_installation: Callable[[int | None], CodeHostPort],
        publish_event: Callable[[str, dict[str, Any]], None],
        options: RepairOptions,
    ):
        self.store = store
        self.policies = policies
        self.fixer = fixer
        self.code_host_for_installation = code_host_for_installation
        self.publish_event = publish_event
        self.options = options

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
            raise ValueError("'original' and 'patched' file maps are required")
        original = {
            path: text
            for path, text in original_files.items()
            if isinstance(path, str) and isinstance(text, str)
        }
        patched = {
            path: text
            for path, text in patched_files.items()
            if isinstance(path, str) and isinstance(text, str)
        }
        total = sum(
            len(text.encode("utf-8")) for text in list(original.values()) + list(patched.values())
        )
        if total > self.options.max_diff_bytes * 10:
            raise ValueError("proof payload exceeds the maximum analysable size")
        result = ProofRunner(self._proof_verifier).prove(
            original,
            patched,
            str(reproduction_command or ""),
            str(regression_command or ""),
        )
        metrics.inc("proof_runs_total")
        return result

    def create_fix(
        self,
        task_id: str,
        installation_id: int | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        task = self.store.get(task_id, tenant_id)
        if not task or not task.get("report"):
            raise ValueError("completed task not found")
        if task.get("pull_request") is None:
            raise ValueError("fix commits require a GitHub pull request task")
        actual_tenant = task.get("tenant_id") or tenant_id or "default"
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
            return dict(receipt.get("result") or {})
        if receipt["status"] == "busy":
            raise RuntimeError("a repair publication for this task is already in progress")
        try:
            result = self.fixer.create_fix_commits(
                self.code_host_for_installation(installation_id),
                task["repository"],
                task["pull_request"],
                task["report"],
                operation_key=effect_key,
                allowed_rule_ids=allowed_rule_ids,
            )
            if not self.store.complete_effect(effect_key, owner, result):
                raise RuntimeError("repair publication lease was lost before completion")
        except Exception as exc:
            self.store.release_effect(effect_key, owner, str(exc))
            raise
        metrics.inc("fix_runs_total")
        self.publish_event(
            "fix.completed",
            {
                "task_id": task_id,
                "tenant_id": actual_tenant,
                "repository": task["repository"],
                "published": bool(result.get("branch")),
                "commits": len(result.get("commits", [])),
            },
        )
        return result
