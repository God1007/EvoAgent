"""Public-safe client errors and message-free operational failure summaries.

Adapters and infrastructure code must use their own errors. Inheriting from the
built-in classes preserves compatibility for callers while letting the HTTP edge
distinguish reviewed client messages from arbitrary exception text.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.-]+")
_SUMMARY = re.compile(
    r"^(?P<operation>[a-z][a-z0-9 -]{0,63}) "
    r"\[type=(?P<error_type>[A-Za-z0-9_.-]{1,160}); ref=(?P<error_ref>[0-9a-f]{16})\]$"
)
_OPERATIONS = frozenset(
    {
        "operation failed",
        "proof executor failed",
        "verification launch failed",
        "review agent failed",
        "review execution failed",
        "review node failed",
        "external effect failed",
        "task delivery failed",
        "queue dependency failed",
        "outbox dispatch failed",
        "store readiness failed",
        "outbox readiness failed",
        "shadow review failed",
        "evaluation case failed",
        "plugin listener failed",
        "plugin activation failed",
        "plugin shutdown failed",
    }
)
_UNCLASSIFIED_REF = hashlib.sha256(b"evoagent:unclassified-failure").hexdigest()[:16]


class ClientInputError(ValueError):
    """An expected request-validation failure with a client-safe message."""


class AccessDeniedError(PermissionError):
    """An expected authorization/policy denial with a client-safe message."""


def exception_type(error: BaseException) -> str:
    """Return a bounded identifier without inspecting the exception message."""
    qualified = "%s.%s" % (type(error).__module__, type(error).__qualname__)
    return _SAFE_TOKEN.sub("_", qualified)[:160] or "unknown"


def exception_reference(error: BaseException) -> str:
    """Fingerprint type + traceback locations, never values or error text."""
    frames: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = _SAFE_TOKEN.sub("_", str(frame.f_globals.get("__name__", "")))[:120]
        function = _SAFE_TOKEN.sub("_", frame.f_code.co_name)[:120]
        frames.append("%s:%s:%d" % (module, function, traceback.tb_lineno))
        traceback = traceback.tb_next
    signature = "|".join((exception_type(error), *frames[-12:]))
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def safe_exception_fields(error: BaseException) -> dict[str, str]:
    return {
        "error_type": exception_type(error),
        "error_ref": exception_reference(error),
    }


def safe_exception_summary(error: BaseException, operation: str = "operation failed") -> str:
    """Create a persistable/displayable summary containing no exception text."""
    operation = operation if operation in _OPERATIONS else "operation failed"
    fields = safe_exception_fields(error)
    return "%s [type=%s; ref=%s]" % (
        operation,
        fields["error_type"],
        fields["error_ref"],
    )


def coerce_safe_summary(value: Any, operation: str) -> str:
    """Accept only an internally shaped summary for the expected operation."""
    operation = operation if operation in _OPERATIONS else "operation failed"
    match = _SUMMARY.fullmatch(value) if isinstance(value, str) else None
    if match is not None and match.group("operation") == operation:
        return value
    return "%s [type=unknown; ref=%s]" % (operation, _UNCLASSIFIED_REF)


def preserve_safe_summary(value: Any, fallback_operation: str) -> str:
    """Preserve any recognized internal summary or replace untrusted text.

    Persistence adapters use this at their boundary because the operation may
    have been classified by an upstream component (for example, a task can fail
    because its queue delivery was exhausted). Arbitrary strings are never
    passed through.
    """
    fallback_operation = (
        fallback_operation if fallback_operation in _OPERATIONS else "operation failed"
    )
    match = _SUMMARY.fullmatch(value) if isinstance(value, str) else None
    if match is not None and match.group("operation") in _OPERATIONS:
        return value
    return "%s [type=unknown; ref=%s]" % (fallback_operation, _UNCLASSIFIED_REF)
