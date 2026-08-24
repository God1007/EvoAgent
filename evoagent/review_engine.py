"""Replaceable default implementation of the EvoAgent review workflow."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .agents import MAX_REVIEW_AGENTS, MultiAgentCoordinator
from .config import Settings
from .harness import ReviewHarness
from .model_gateway import ModelGovernanceContext
from .observability import Observability
from .policy import RepositoryPolicyResolver
from .ports import ModelGatewayPort, ReviewWorkflowStorePort
from .review_extensions import ReviewerContribution
from .reviewer import GatewayReviewer, Reviewer
from .skills import SkillRegistry


def _source_revision(root: Path) -> str:
    inventory = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
        if path.is_file()
    }
    if not inventory:
        raise RuntimeError("application source inventory is unavailable")
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_APPLICATION_SOURCE_REVISION = _source_revision(Path(__file__).parent)


class ReviewEngine:
    """Own the reviewer graph, model adapter, harness, and Skill registry."""

    def __init__(
        self,
        settings: Settings,
        store: ReviewWorkflowStorePort,
        observability: Observability,
        model_gateway: ModelGatewayPort,
        reviewer_contributions: Sequence[ReviewerContribution],
    ):
        self.settings = settings
        self.store = store
        self.observability = observability
        self.model_gateway = model_gateway
        self.llm_config = model_gateway.route_info()
        if len(reviewer_contributions) + int(model_gateway.configured) > MAX_REVIEW_AGENTS:
            raise ValueError("review graph accepts at most %d agents" % MAX_REVIEW_AGENTS)
        self._reviewer_contributions = tuple(reviewer_contributions)
        contribution_ids = Counter(item.contribution_id for item in self._reviewer_contributions)
        duplicates = sorted(
            contribution_id for contribution_id, count in contribution_ids.items() if count > 1
        )
        if duplicates:
            raise ValueError("duplicate reviewer contribution ids: %s" % ", ".join(duplicates))
        self.registry: SkillRegistry
        self._execution: tuple[str, tuple[Reviewer, ...], ReviewHarness]
        self._load()

    @property
    def llm_available(self) -> bool:
        return self.model_gateway.configured

    def build_llm_reviewer(self, prompt: str = "") -> GatewayReviewer:
        if not self.model_gateway.configured:
            raise RuntimeError("no LLM provider is configured")
        return GatewayReviewer(self.model_gateway, self._task_context, prompt)

    def _task_context(self, task_id: str) -> ModelGovernanceContext:
        task = self.store.get(task_id) if task_id else None
        if not task:
            raise ValueError("model review task context is unavailable")
        policy = RepositoryPolicyResolver.from_snapshot(
            (task.get("input") or {}).get("repository_policy")
        )
        return ModelGovernanceContext(
            tenant_id=str(task.get("tenant_id") or "default"),
            repository=str(task["repository"]),
            allowed_providers=policy.allowed_llm_providers,
            allowed_models=policy.allowed_llm_models,
            required_region=policy.llm_region,
        )

    def build_coordinator(
        self, reviewers: list[Reviewer], *, persist_messages: bool = True
    ) -> MultiAgentCoordinator:
        return MultiAgentCoordinator(
            reviewers,
            store=self.store if persist_messages else None,
            timeout_seconds=self.settings.timeout_seconds,
        )

    def build_evaluation_reviewer(self, prompt: str) -> Reviewer:
        return self.build_coordinator(
            [self.build_llm_reviewer(prompt)],
            persist_messages=False,
        )

    def build_harness(self, reviewer: Reviewer) -> ReviewHarness:
        return ReviewHarness(
            self.store,
            reviewer,
            self.settings.max_steps,
            self.settings.timeout_seconds,
            observability=self.observability,
        )

    def execution_snapshot(self) -> tuple[str, tuple[Reviewer, ...], ReviewHarness]:
        return self._execution

    def inventory_snapshot(self) -> tuple[list[dict], str]:
        return self.registry.list(), self._execution[0]

    def execution_revision(self) -> str:
        return self.execution_snapshot()[0]

    def _load(self) -> None:
        registry = SkillRegistry(
            self.settings.skills_dir,
            self.settings.skill_sandbox,
            self.settings.skill_timeout_seconds,
            self.settings.skill_memory_mb,
            self.settings.skill_signing_key,
            self.settings.skill_container_image,
            self.settings.skill_require_container,
        )
        for contribution in self._reviewer_contributions:
            registry.register(
                contribution.contribution_id,
                contribution.reviewer,
                contribution.version,
                contribution.description,
                contribution.source,
            )
        if self.model_gateway.configured:
            active = self.store.get_active_skill_version("llm-review")
            registry.register(
                "llm-review",
                self.build_llm_reviewer(active["prompt"] if active else ""),
                "1.0.0",
                "Context-aware AI code review via %s" % self.llm_config["provider"],
            )
        registry.reload()
        reviewers = registry.reviewers()
        reviewer = self.build_coordinator(reviewers)
        harness = self.build_harness(reviewer)
        revision = hashlib.sha256(
            json.dumps(
                {
                    "application": {
                        "version": __version__,
                        "source_sha256": _APPLICATION_SOURCE_REVISION,
                    },
                    "review_policy": {
                        "max_steps": self.settings.max_steps,
                        "timeout_seconds": self.settings.timeout_seconds,
                    },
                    "model_gateway": self.model_gateway.execution_revision(),
                    "skills": registry.revision(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.registry = registry
        self._execution = (revision, tuple(reviewers), harness)
