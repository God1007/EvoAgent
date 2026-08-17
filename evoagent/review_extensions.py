"""Stable service-definition types for pluggable review contributors."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .reviewer import Reviewer

_CONTRIBUTION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


@dataclass(frozen=True)
class ReviewerContribution:
    """One trusted reviewer registered into the default coordinator.

    The contribution metadata is deliberately separate from the reviewer
    implementation so the consumer can expose an auditable inventory without
    importing or understanding the provider package.
    """

    contribution_id: str
    reviewer: Reviewer
    version: str = "1.0.0"
    description: str = ""
    source: str = "trusted-plugin"

    def __post_init__(self) -> None:
        if not _CONTRIBUTION_ID.fullmatch(self.contribution_id):
            raise ValueError("invalid reviewer contribution id: %s" % self.contribution_id)
        if not _VERSION.fullmatch(self.version):
            raise ValueError(
                "reviewer contribution %s has invalid semantic version: %s"
                % (self.contribution_id, self.version)
            )
        if not isinstance(self.reviewer, Reviewer):
            raise TypeError("reviewer contribution must contain a Reviewer")
        if not str(self.reviewer.name).strip():
            raise ValueError("reviewer contribution must expose a non-empty reviewer name")
        if not self.source.strip():
            raise ValueError("reviewer contribution source must not be empty")
