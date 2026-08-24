"""Strict JSON parsing for untrusted boundaries."""

import json
from typing import Any


def _reject_constant(_value: str) -> None:
    raise ValueError("JSON contains a non-standard numeric constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON contains duplicate field: %s" % key)
        value[key] = item
    return value


def strict_json_loads(value: str | bytes) -> Any:
    text = value.decode("utf-8") if isinstance(value, bytes) else value
    parsed = json.loads(
        text,
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )
    json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return parsed
