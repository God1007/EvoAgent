"""Stable capability definitions consumed by trusted EvoAgent plugins."""

from __future__ import annotations

from .auth import AuthManager
from .circuit_breaker import CircuitBreaker
from .config import Settings
from .evolution import EvolutionEngine
from .fix_rules import FixRule
from .fixer import SafeFixer
from .observability import AlertManager, Observability
from .plugins import CapabilityKey
from .policy import RepositoryPolicyResolver
from .ports import ApplicationStorePort, CodeHostPort, QueueFactoryPort
from .review_engine import ReviewEngine
from .rollout import ReleaseManager

SETTINGS = CapabilityKey[Settings]("settings")
STORE = CapabilityKey[ApplicationStorePort]("store")
REPOSITORY_POLICY = CapabilityKey[RepositoryPolicyResolver]("policy.repository")
OBSERVABILITY = CapabilityKey[Observability]("observability")
GITHUB_BREAKER = CapabilityKey[CircuitBreaker]("circuit-breaker.github")
LLM_BREAKER = CapabilityKey[CircuitBreaker]("circuit-breaker.llm")
GITHUB_CLIENT = CapabilityKey[CodeHostPort]("codehost.github")
REVIEW_ENGINE = CapabilityKey[ReviewEngine]("review.engine")
FIX_RULE = CapabilityKey[FixRule]("fix.rule", multiple=True)
FIXER = CapabilityKey[SafeFixer]("fixer")
AUTH = CapabilityKey[AuthManager]("auth")
RELEASES = CapabilityKey[ReleaseManager]("releases")
ALERTS = CapabilityKey[AlertManager]("alerts")
EVOLUTION = CapabilityKey[EvolutionEngine]("evolution")
QUEUE_FACTORY = CapabilityKey[QueueFactoryPort]("queue.factory")
