"""Replaceable aggregate, slice, and confidence metrics for evaluation results."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

# The evaluator compares CWE identities, not reviewer-specific rule names.
RULE_TO_CWE = {
    "SEC-EVAL": "CWE-95",
    "SEC-SUBPROCESS-SHELL": "CWE-78",
    "SEC-HARDCODED-SECRET": "CWE-798",
    "SEC-SQL-CONCAT": "CWE-89",
    "REL-EMPTY-EXCEPT": "CWE-703",
    "REL-DEBUG-PRINT": "CWE-532",
    "SEC-PATH-TRAVERSAL": "CWE-22",
    "SEC-YAML-LOAD": "CWE-502",
    "SEC-WEAK-HASH": "CWE-328",
    "SEC-INSECURE-TEMPFILE": "CWE-377",
    "SEC-WEAK-RANDOM": "CWE-330",
    "REL-UNBOUNDED-RETRY": "CWE-835",
    "SEC-ASSERT-AUTH": "CWE-617",
    "SEC-INSECURE-COOKIE": "CWE-614",
    "SEC-PICKLE-LOAD": "CWE-502",
    "REL-FLOAT-MONEY": "CWE-682",
    "REL-NAIVE-DATETIME": "CWE-367",
    "REL-BLOCKING-ASYNC": "CWE-400",
    "REL-NONATOMIC-WRITE": "CWE-362",
    "SEC-OPEN-REDIRECT": "CWE-601",
    "SEC-LOG-FORGING": "CWE-117",
}

EXTENSION_TO_LANGUAGE = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".json": "JSON",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".yaml": "YAML",
    ".yml": "YAML",
}


def languages_for_paths(paths: Iterable[str]) -> list[str]:
    languages = set()
    for path in paths:
        normalized = path.replace("\\", "/").strip().lower()
        normalized = normalized[2:] if normalized.startswith(("a/", "b/")) else normalized
        extension = "." + normalized.rsplit(".", 1)[-1] if "." in normalized else ""
        languages.add(EXTENSION_TO_LANGUAGE.get(extension, "Other"))
    return sorted(languages or {"Other"})


def serializable_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if math.isfinite(confidence) else None


def empty_totals() -> dict[str, int]:
    return {
        "cases": 0,
        "risk_cases": 0,
        "clean_cases": 0,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "severity_hits": 0,
        "high_total": 0,
        "high_hits": 0,
        "clean_hits": 0,
        "execution_successes": 0,
        "repair_eligible": 0,
        "repair_attempted": 0,
        "repair_passed": 0,
        "repair_abstained": 0,
        "e2e_successes": 0,
    }


def accumulate(totals: dict[str, int], result: dict[str, Any]) -> None:
    totals["cases"] += 1
    totals["risk_cases"] += int(result["expected"] > 0)
    totals["clean_cases"] += int(result["expected"] == 0)
    for field in (
        "tp",
        "fp",
        "fn",
        "severity_hits",
        "high_total",
        "high_hits",
        "repair_eligible",
        "repair_attempted",
        "repair_passed",
        "repair_abstained",
    ):
        totals[field] += int(result[field])
    totals["clean_hits"] += int(result["clean_hit"])
    totals["execution_successes"] += int(result["execution_success"])
    totals["e2e_successes"] += int(result["e2e_success"])


def metric_summary(totals: dict[str, int]) -> dict[str, Any]:
    def ratio(numerator: int, denominator: int, empty: float = 1.0) -> float:
        return round(numerator / denominator, 4) if denominator else empty

    precision = ratio(totals["tp"], totals["tp"] + totals["fp"], 0.0)
    recall = ratio(totals["tp"], totals["tp"] + totals["fn"], 1.0)
    f1 = round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0
    return {
        **totals,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "severity_accuracy": ratio(totals["severity_hits"], totals["tp"]),
        "high_risk_recall": ratio(totals["high_hits"], totals["high_total"]),
        "clean_accuracy": ratio(totals["clean_hits"], totals["clean_cases"]),
        "execution_success_rate": ratio(totals["execution_successes"], totals["cases"], 0.0),
        "safe_fix_rate": ratio(totals["repair_passed"], totals["repair_eligible"], 0.0),
        "e2e_security_fix_rate": ratio(totals["e2e_successes"], totals["risk_cases"], 0.0),
    }


def language_slices(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    languages = sorted(
        {str(language) for result in results for language in result.get("languages", [])}
    )
    slices = {}
    for language in languages:
        totals = empty_totals()
        for result in results:
            if language in result.get("languages", []):
                accumulate(totals, result)
        slices[language] = metric_summary(totals)
    return slices


def cwe_slices(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    cwes = sorted(
        {
            str(cwe)
            for result in results
            for cwe in (
                list(result.get("expected_cwes", []))
                + [item.get("cwe", "") for item in result.get("predictions", [])]
            )
            if cwe
        }
    )
    slices = {}
    for cwe in cwes:
        expected = sum(
            sum(str(item) == cwe for item in result.get("expected_cwes", [])) for result in results
        )
        predicted = sum(
            sum(str(item.get("cwe")) == cwe for item in result.get("predictions", []))
            for result in results
        )
        true_positives = sum(
            sum(str(item.get("cwe")) == cwe for item in result.get("matches", []))
            for result in results
        )
        precision = round(true_positives / predicted, 4) if predicted else 0.0
        recall = round(true_positives / expected, 4) if expected else 1.0
        f1 = round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0
        slices[cwe] = {
            "expected": expected,
            "predicted": predicted,
            "tp": true_positives,
            "fp": predicted - true_positives,
            "fn": expected - true_positives,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return slices


def rule_slices(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    predictions = [prediction for result in results for prediction in result.get("predictions", [])]
    rules = sorted({str(item.get("rule_id", "")) for item in predictions if item.get("rule_id")})
    slices = {}
    for rule in rules:
        selected = [item for item in predictions if item.get("rule_id") == rule]
        true_positives = sum(bool(item.get("matched")) for item in selected)
        valid_confidences = [
            float(item["confidence"])
            for item in selected
            if item.get("confidence") is not None and 0 <= float(item["confidence"]) <= 1
        ]
        slices[rule] = {
            "predicted": len(selected),
            "tp": true_positives,
            "fp": len(selected) - true_positives,
            "precision": round(true_positives / len(selected), 4) if selected else 0.0,
            "mean_confidence": (
                round(sum(valid_confidences) / len(valid_confidences), 4)
                if valid_confidences
                else None
            ),
        }
    return slices


def confidence_calibration(results: list[dict[str, Any]]) -> dict[str, Any]:
    predictions = [prediction for result in results for prediction in result.get("predictions", [])]
    values = []
    invalid = 0
    for prediction in predictions:
        confidence = prediction.get("confidence")
        if confidence is None or not 0 <= float(confidence) <= 1:
            invalid += 1
            continue
        values.append((float(confidence), int(bool(prediction.get("matched")))))
    bins = []
    weighted_gap = 0.0
    for index in range(10):
        lower = index / 10
        upper = (index + 1) / 10
        selected = []
        for value in values:
            in_bin = lower <= value[0] <= upper if index == 9 else lower <= value[0] < upper
            if in_bin:
                selected.append(value)
        mean_confidence = (
            round(sum(value[0] for value in selected) / len(selected), 4) if selected else None
        )
        accuracy = (
            round(sum(value[1] for value in selected) / len(selected), 4) if selected else None
        )
        gap = (
            abs(float(mean_confidence) - float(accuracy))
            if mean_confidence is not None and accuracy is not None
            else 0.0
        )
        weighted_gap += len(selected) * gap
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(selected),
                "mean_confidence": mean_confidence,
                "accuracy": accuracy,
            }
        )
    brier = (
        round(sum((confidence - outcome) ** 2 for confidence, outcome in values) / len(values), 4)
        if values
        else None
    )
    return {
        "scope": "reported-finding correctness",
        "predictions": len(predictions),
        "valid_confidences": len(values),
        "invalid_confidences": invalid,
        "expected_calibration_error": round(weighted_gap / len(values), 4) if values else None,
        "brier_score": brier,
        "bins": bins,
    }
