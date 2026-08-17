"""Versioned tenant/repository policy resolution and enforcement."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from .ports import RepositoryPolicyStorePort

_POLICY_FIELDS = frozenset(
    {
        "enabled",
        "auto_fix",
        "post_review_comments",
        "allowed_reviewers",
        "allowed_fix_rules",
        "allowed_llm_providers",
        "allowed_llm_models",
        "llm_region",
        "max_diff_bytes",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


def _bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError("repository policy %s must be a boolean" % field)
    return value


def _identifiers(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("repository policy %s must be a list of strings" % field)
    normalized = []
    for item in value:
        item = item.strip()
        if not _IDENTIFIER.fullmatch(item):
            raise ValueError("repository policy %s contains an invalid identifier" % field)
        normalized.append(item)
    if len(normalized) != len(set(normalized)):
        raise ValueError("repository policy %s contains duplicates" % field)
    if len(normalized) > 100:
        raise ValueError("repository policy %s cannot contain more than 100 entries" % field)
    return tuple(sorted(normalized))


def _optional_identifier(value: Any, field: str) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.strip()):
        raise ValueError("repository policy %s must be a valid identifier or null" % field)
    return value.strip()


@dataclass(frozen=True)
class RepositoryPolicy:
    enabled: bool = True
    auto_fix: bool = False
    post_review_comments: bool = True
    allowed_reviewers: tuple[str, ...] = ()
    allowed_fix_rules: tuple[str, ...] = ()
    allowed_llm_providers: tuple[str, ...] = ()
    allowed_llm_models: tuple[str, ...] = ()
    llm_region: str = ""
    max_diff_bytes: int | None = None
    version: int = 0
    source: str = "default"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RepositoryPolicy:
        if not isinstance(value, dict):
            raise ValueError("repository policy must be an object")
        unknown = set(value).difference(_POLICY_FIELDS)
        if unknown:
            raise ValueError(
                "unsupported repository policy fields: %s" % ", ".join(sorted(unknown))
            )
        max_diff_bytes = value.get("max_diff_bytes")
        if max_diff_bytes is not None and (
            isinstance(max_diff_bytes, bool)
            or not isinstance(max_diff_bytes, int)
            or max_diff_bytes <= 0
        ):
            raise ValueError("repository policy max_diff_bytes must be a positive integer or null")
        return cls(
            enabled=_bool(value.get("enabled"), "enabled", True),
            auto_fix=_bool(value.get("auto_fix"), "auto_fix", False),
            post_review_comments=_bool(
                value.get("post_review_comments"), "post_review_comments", True
            ),
            allowed_reviewers=_identifiers(value.get("allowed_reviewers"), "allowed_reviewers"),
            allowed_fix_rules=_identifiers(value.get("allowed_fix_rules"), "allowed_fix_rules"),
            allowed_llm_providers=_identifiers(
                value.get("allowed_llm_providers"), "allowed_llm_providers"
            ),
            allowed_llm_models=_identifiers(value.get("allowed_llm_models"), "allowed_llm_models"),
            llm_region=_optional_identifier(value.get("llm_region"), "llm_region"),
            max_diff_bytes=max_diff_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_fix": self.auto_fix,
            "post_review_comments": self.post_review_comments,
            "allowed_reviewers": list(self.allowed_reviewers),
            "allowed_fix_rules": list(self.allowed_fix_rules),
            "allowed_llm_providers": list(self.allowed_llm_providers),
            "allowed_llm_models": list(self.allowed_llm_models),
            "llm_region": self.llm_region or None,
            "max_diff_bytes": self.max_diff_bytes,
        }


class RepositoryPolicyResolver:
    """Resolve one immutable decision object at a use-case boundary."""

    def __init__(self, store: RepositoryPolicyStorePort):
        self.store = store

    def resolve(self, tenant_id: str, repository: str) -> RepositoryPolicy:
        record = self.store.get_repository_policy(tenant_id, repository)
        if record is not None:
            policy = RepositoryPolicy.from_dict(record["policy"])
            return replace(policy, version=int(record["version"]), source="configured")
        return RepositoryPolicy(
            enabled=self.store.repository_allowed(tenant_id, repository),
            auto_fix=self.store.repository_allowed(tenant_id, repository, True),
            source="legacy-grant",
        )

    def from_snapshot(self, value: Any) -> RepositoryPolicy:
        if not isinstance(value, dict):
            raise ValueError("task does not contain a valid repository policy snapshot")
        policy = RepositoryPolicy.from_dict(value.get("policy") or {})
        version = value.get("version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise ValueError("task repository policy version is invalid")
        return replace(policy, version=version, source="task-snapshot")

    def save(
        self,
        tenant_id: str,
        repository: str,
        raw_policy: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if not tenant_id or len(tenant_id) > 200:
            raise ValueError("tenant_id is required and must be at most 200 characters")
        if not repository or len(repository) > 250:
            raise ValueError("repository is required and must be at most 250 characters")
        if not actor or len(actor) > 200:
            raise ValueError("policy actor is required and must be at most 200 characters")
        policy = RepositoryPolicy.from_dict(raw_policy)
        return self.store.save_repository_policy(tenant_id, repository, policy.to_dict(), actor)

    @staticmethod
    def authorize_review(
        policy: RepositoryPolicy,
        diff_bytes: int,
        reviewer: str,
        llm_provider: str,
        llm_model: str,
        llm_routes: tuple[dict[str, str], ...] | None = None,
    ) -> None:
        if not policy.enabled:
            raise PermissionError("repository is disabled by tenant policy")
        if policy.max_diff_bytes is not None and diff_bytes > policy.max_diff_bytes:
            raise ValueError(
                "diff exceeds repository policy limit of %d bytes" % policy.max_diff_bytes
            )
        if policy.allowed_reviewers and reviewer not in policy.allowed_reviewers:
            raise PermissionError("reviewer '%s' is not allowed by repository policy" % reviewer)
        if llm_routes is not None:
            eligible = [
                route
                for route in llm_routes
                if (
                    not policy.allowed_llm_providers
                    or route.get("provider") in policy.allowed_llm_providers
                )
                and (
                    not policy.allowed_llm_models or route.get("model") in policy.allowed_llm_models
                )
                and (not policy.llm_region or route.get("region") == policy.llm_region)
            ]
            if not eligible:
                raise PermissionError("no configured model route satisfies repository policy")
            return
        if policy.llm_region:
            raise PermissionError("repository model region requires a routing-aware gateway")
        if policy.allowed_llm_providers and llm_provider not in policy.allowed_llm_providers:
            raise PermissionError(
                "LLM provider '%s' is not allowed by repository policy" % llm_provider
            )
        if policy.allowed_llm_models and llm_model not in policy.allowed_llm_models:
            raise PermissionError("LLM model '%s' is not allowed by repository policy" % llm_model)

    @staticmethod
    def authorize_fix(
        policy: RepositoryPolicy, available_rule_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not policy.enabled or not policy.auto_fix:
            raise PermissionError("automatic repair is not enabled by repository policy")
        if not policy.allowed_fix_rules:
            return available_rule_ids
        unknown = set(policy.allowed_fix_rules).difference(available_rule_ids)
        if unknown:
            raise ValueError(
                "repository policy references unavailable fix rules: %s"
                % ", ".join(sorted(unknown))
            )
        return policy.allowed_fix_rules

    @staticmethod
    def snapshot(policy: RepositoryPolicy) -> dict[str, Any]:
        return {"version": policy.version, "policy": policy.to_dict()}
