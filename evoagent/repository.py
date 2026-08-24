"""Canonical GitHub repository identity."""

import re

from .errors import ClientInputError

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def canonical_repository(repository: str) -> str:
    if (
        not isinstance(repository, str)
        or not _REPOSITORY.fullmatch(repository)
        or any(part in {".", ".."} for part in repository.split("/"))
    ):
        raise ClientInputError("repository must use the GitHub owner/name form")
    return repository.lower()
