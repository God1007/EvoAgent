"""Explicit errors whose messages are safe to expose at a public boundary.

Adapters and infrastructure code must use their own errors. Inheriting from the
built-in classes preserves compatibility for callers while letting the HTTP edge
distinguish reviewed client messages from arbitrary exception text.
"""


class ClientInputError(ValueError):
    """An expected request-validation failure with a client-safe message."""


class AccessDeniedError(PermissionError):
    """An expected authorization/policy denial with a client-safe message."""
