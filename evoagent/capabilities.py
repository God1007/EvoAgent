"""Stable capability definitions consumed by trusted EvoAgent plugins."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from .auth import AuthManager
from .circuit_breaker import CircuitBreaker
from .config import Settings
from .evolution import EvolutionEngine
from .fix_rules import FixRule
from .fixer import SafeFixer
from .github import GitHubClient
from .observability import AlertManager, Observability
from .plugins import CapabilityKey
from .review_engine import ReviewEngine
from .rollout import ReleaseManager
from .task_queue import TaskQueue

SETTINGS = CapabilityKey[Settings]("settings")
STORE = CapabilityKey[Any]("store")
OBSERVABILITY = CapabilityKey[Observability]("observability")
GITHUB_BREAKER = CapabilityKey[CircuitBreaker]("circuit-breaker.github")
LLM_BREAKER = CapabilityKey[CircuitBreaker]("circuit-breaker.llm")
GITHUB_CLIENT = CapabilityKey[GitHubClient]("codehost.github")
REVIEW_ENGINE = CapabilityKey[ReviewEngine]("review.engine")
FIX_RULE = CapabilityKey[FixRule]("fix.rule", multiple=True)
FIXER = CapabilityKey[SafeFixer]("fixer")
AUTH = CapabilityKey[AuthManager]("auth")
RELEASES = CapabilityKey[ReleaseManager]("releases")
ALERTS = CapabilityKey[AlertManager]("alerts")
EVOLUTION = CapabilityKey[EvolutionEngine]("evolution")
QUEUE_FACTORY = CapabilityKey["QueueFactory"]("queue.factory")


class QueueFactory(Protocol):
    def create(
        self,
        handler: Callable[[dict[str, Any]], None],
        on_dead_letter: Callable[[dict[str, Any], str], None],
    ) -> TaskQueue: ...
