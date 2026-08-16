"""Replaceable default implementation of the EvoAgent review workflow."""

from __future__ import annotations

from .agents import FilteredAgent, MultiAgentCoordinator
from .circuit_breaker import CircuitBreaker
from .config import Settings
from .harness import ReviewHarness
from .observability import Observability
from .ports import ReviewWorkflowStorePort
from .reviewer import LocalRuleReviewer, OpenAICompatibleReviewer, Reviewer
from .skills import SkillRegistry


class ReviewEngine:
    """Own the reviewer graph, model adapter, harness, and Skill registry."""

    def __init__(
        self,
        settings: Settings,
        store: ReviewWorkflowStorePort,
        observability: Observability,
        llm_breaker: CircuitBreaker,
    ):
        self.settings = settings
        self.store = store
        self.observability = observability
        self.llm_breaker = llm_breaker
        self.llm_config = settings.resolved_llm()
        self.registry = SkillRegistry(
            settings.skills_dir,
            settings.skill_sandbox,
            settings.skill_timeout_seconds,
            settings.skill_memory_mb,
            settings.skill_signing_key,
            settings.skill_container_image,
        )
        local = LocalRuleReviewer()
        self.registry.register(
            "security-review",
            FilteredAgent("security-agent", local, ("SEC-",)),
            "1.0.0",
            "Security, injection and secret detection",
        )
        self.registry.register(
            "reliability-review",
            FilteredAgent("reliability-agent", local, ("REL-",)),
            "1.0.0",
            "Reliability and observability review",
        )
        self.reviewer: Reviewer
        self.harness: ReviewHarness
        self.reload()

    @property
    def llm_available(self) -> bool:
        return bool(self.llm_config)

    def build_llm_reviewer(self, prompt: str = "") -> OpenAICompatibleReviewer:
        if not self.llm_config:
            raise RuntimeError("no LLM provider is configured")
        return OpenAICompatibleReviewer(
            str(self.llm_config["base_url"]),
            str(self.llm_config["api_key"]),
            str(self.llm_config["model"]),
            self.settings.timeout_seconds,
            system_prompt=prompt,
            provider=str(self.llm_config["provider"]),
            extra_headers=dict(self.llm_config.get("headers") or {}),
            breaker=self.llm_breaker,
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
        if self.llm_config:
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
