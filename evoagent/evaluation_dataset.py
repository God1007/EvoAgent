"""Canonical dataset identity shared by compilers, audits, and evaluators."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .diff_parser import parse_unified_diff
from .json_boundary import strict_json_loads

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def normalized_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    return value[2:] if value.startswith(("a/", "b/")) else value


def validate_case(case: dict[str, Any], line_number: int = 0) -> None:
    prefix = "dataset line %d" % line_number if line_number else "evaluation case"
    for field in ("id", "repository", "pull_request", "split", "diff", "expected_findings"):
        if field not in case:
            raise ValueError("%s is missing %s" % (prefix, field))
    if case["split"] not in {"validation", "holdout"}:
        raise ValueError("%s has invalid split" % prefix)
    parsed = parse_unified_diff(str(case["diff"]))
    if not parsed.files or not parsed.added_lines:
        raise ValueError("%s does not contain a scoreable unified diff" % prefix)
    if not isinstance(case["expected_findings"], list):
        raise ValueError("%s expected_findings must be an array" % prefix)
    added_locations = {(normalized_path(item.path), int(item.line)) for item in parsed.added_lines}
    for expected in case["expected_findings"]:
        if not isinstance(expected, dict):
            raise ValueError("%s findings must be JSON objects" % prefix)
        for field in ("path", "start_line", "end_line", "cwe", "severity"):
            if field not in expected:
                raise ValueError("%s finding is missing %s" % (prefix, field))
        if str(expected["severity"]).lower() not in SEVERITY_RANK:
            raise ValueError("%s finding has invalid severity" % prefix)
        try:
            start_line = int(expected["start_line"])
            end_line = int(expected["end_line"])
        except (TypeError, ValueError) as exc:
            raise ValueError("%s finding line range must be integral" % prefix) from exc
        if start_line < 1 or end_line < 1:
            raise ValueError("%s finding line range must be positive" % prefix)
        if start_line > end_line:
            raise ValueError("%s finding has an inverted line range" % prefix)
        expected_path = normalized_path(str(expected["path"]))
        if not any(
            path == expected_path and start_line <= line <= end_line
            for path, line in added_locations
        ):
            raise ValueError("%s finding does not cover an added line" % prefix)
    after_files = case.get("after_files", {})
    if not isinstance(after_files, dict) or any(
        not isinstance(path, str) or not isinstance(content, str)
        for path, content in after_files.items()
    ):
        raise ValueError("%s after_files must map string paths to string contents" % prefix)
    repair = case.get("repair_validation", {})
    if not isinstance(repair, dict):
        raise ValueError("%s repair_validation must be an object" % prefix)
    auto_fixable = repair.get("auto_fixable", False)
    risk_pattern = repair.get("risk_pattern", "")
    required_patterns = repair.get("required_after_patterns", [])
    if type(auto_fixable) is not bool:
        raise ValueError("%s auto_fixable must be a boolean" % prefix)
    if not isinstance(risk_pattern, str) or (auto_fixable and not risk_pattern):
        raise ValueError("%s repair risk_pattern must be a non-empty string" % prefix)
    if not isinstance(required_patterns, list) or any(
        not isinstance(pattern, str) for pattern in required_patterns
    ):
        raise ValueError("%s required_after_patterns must contain only strings" % prefix)


def load_jsonl(path: str) -> list[dict[str, Any]]:
    cases = []
    with open(path, encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                case = strict_json_loads(raw)
            except (ValueError, RecursionError) as exc:
                raise ValueError("invalid JSON on line %d: %s" % (line_number, exc)) from exc
            if not isinstance(case, dict):
                raise ValueError("dataset line %d must be a JSON object" % line_number)
            validate_case(case, line_number)
            cases.append(case)
    ids = [str(case["id"]) for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("dataset contains duplicate case ids")
    return cases


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dataset_fingerprint(cases: Iterable[dict]) -> str:
    """Fingerprint exactly what is scored, independent of JSONL formatting."""
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda item: str(item["id"])):
        digest.update(canonical_json(case).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
