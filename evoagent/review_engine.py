"""Replaceable default implementation of the EvoAgent review workflow."""

from __future__ import annotations

from collections.abc import Sequence

from .agents import MultiAgentCoordinator
from .config import Settings
from .harness import ReviewHarness
from .model_gateway import ModelGovernanceContext
from .observability import Observability
from .plugins import PluginConfigurationError
from .ports import ModelGatewayPort, ReviewWorkflowStorePort
from .review_extensions import ReviewerContribution
from .reviewer import GatewayReviewer, Reviewer
from .skills import SkillRegistry


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
        self.registry = SkillRegistry(
            settings.skills_dir,
            settings.skill_sandbox,
            settings.skill_timeout_seconds,
            settings.skill_memory_mb,
            settings.skill_signing_key,
            settings.skill_container_image,
            settings.skill_require_container,
        )
        contribution_ids = [item.contribution_id for item in reviewer_contributions]
        duplicates = sorted(
            contribution_id
            for contribution_id in set(contribution_ids)
            if contribution_ids.count(contribution_id) > 1
        )
        if duplicates:
            raise PluginConfigurationError(
                "duplicate reviewer contribution ids: %s" % ", ".join(duplicates)
            )
        for contribution in reviewer_contributions:
            self.registry.register(
                contribution.contribution_id,
                contribution.reviewer,
                contribution.version,
                contribution.description,
                contribution.source,
            )
        self.reviewer: Reviewer
        self.harness: ReviewHarness
        self.reload()

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
        snapshot = ((task.get("input") or {}).get("repository_policy") or {}).get("policy") or {}
        return ModelGovernanceContext(
            tenant_id=str(task.get("tenant_id") or "default"),
            repository=str(task["repository"]),
            allowed_providers=tuple(snapshot.get("allowed_llm_providers") or ()),
            allowed_models=tuple(snapshot.get("allowed_llm_models") or ()),
            required_region=str(snapshot.get("llm_region") or ""),
        )

    def build_coordinator(self, reviewers: list[Reviewer]) -> MultiAgentCoordinator:
        return MultiAgentCoordinator(reviewers, store=self.store)

    def build_harness(self, reviewer: Reviewer) -> ReviewHarness:
        return ReviewHarness(
            self.store,
            reviewer,
            self.settings.max_steps,
            self.settings.timeout_seconds,
            observability=self.observability,
        )

    def reload(self) -> list[dict]:
        if self.model_gateway.configured:
            active = self.store.get_active_skill_version("llm-review")
            self.registry.register(
                "llm-review",
                self.build_llm_reviewer(active["prompt"] if active else ""),
                "1.0.0",
                "Context-aware AI code review via %s" % self.llm_config["provider"],
            )
        skills = self.registry.reload()
        self.reviewer = self.build_coordinator(self.registry.reviewers())
        self.harness = self.build_harness(self.reviewer)
        return skills
