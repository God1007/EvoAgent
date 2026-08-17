"""Versioned repository-policy governance use cases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import ClientInputError
from ..policy import RepositoryPolicy, RepositoryPolicyResolver
from ..ports import RepositoryPolicyStorePort


class PolicyUseCases:
    def __init__(
        self,
        store: RepositoryPolicyStorePort,
        policies: RepositoryPolicyResolver,
        available_fix_rules: Callable[[], tuple[str, ...]],
    ):
        self.store = store
        self.policies = policies
        self.available_fix_rules = available_fix_rules

    def get_repository_policy(self, tenant_id: str, repository: str) -> dict[str, Any]:
        policy = self.policies.resolve(tenant_id, repository)
        return {
            "tenant_id": tenant_id,
            "repository": repository,
            "version": policy.version,
            "source": policy.source,
            "policy": policy.to_dict(),
            "history": self.store.list_repository_policy_versions(tenant_id, repository, 50),
        }

    def set_repository_policy(
        self,
        tenant_id: str,
        repository: str,
        policy: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        try:
            parsed = RepositoryPolicy.from_dict(policy)
        except ValueError as exc:
            raise ClientInputError(str(exc)) from None
        unknown_rules = set(parsed.allowed_fix_rules).difference(self.available_fix_rules())
        if unknown_rules:
            raise ClientInputError(
                "repository policy references unavailable fix rules: %s"
                % ", ".join(sorted(unknown_rules))
            )
        return self.policies.save(tenant_id, repository, parsed.to_dict(), actor)
