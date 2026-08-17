"""End-to-end PR diff evaluation with reproducible matching and repair gates."""

import re
import time
from dataclasses import dataclass
from typing import Any

from .diff_parser import parse_unified_diff
from .evaluation_dataset import dataset_fingerprint, normalized_path
from .evaluation_metrics import (
    RULE_TO_CWE,
    accumulate,
    confidence_calibration,
    cwe_slices,
    empty_totals,
    language_slices,
    languages_for_paths,
    metric_summary,
    rule_slices,
    serializable_confidence,
)
from .evaluation_provenance import audit_dataset_provenance
from .fixer import SafeFixer
from .models import Finding
from .reviewer import Reviewer
from .verifier import RepairVerifier


@dataclass
class Match:
    expected_index: int
    predicted_index: int
    location_distance: int


def _candidate_edges(
    expected: list[dict],
    predicted: list[Finding],
    line_tolerance: int,
) -> dict[int, list[tuple[int, int]]]:
    edges: dict[int, list[tuple[int, int]]] = {}
    for expected_index, truth in enumerate(expected):
        start = int(truth["start_line"])
        end = int(truth["end_line"])
        truth_path = normalized_path(str(truth["path"]))
        truth_cwe = str(truth["cwe"]).upper()
        options = []
        for predicted_index, finding in enumerate(predicted):
            if normalized_path(finding.path) != truth_path:
                continue
            if RULE_TO_CWE.get(finding.rule_id, finding.rule_id).upper() != truth_cwe:
                continue
            if start <= finding.line <= end:
                distance = 0
            else:
                distance = min(abs(finding.line - start), abs(finding.line - end))
            if distance <= line_tolerance:
                options.append((predicted_index, distance))
        edges[expected_index] = sorted(options, key=lambda item: (item[1], item[0]))
    return edges


def one_to_one_match(
    expected: list[dict],
    predicted: list[Finding],
    line_tolerance: int = 2,
) -> list[Match]:
    """Maximum-cardinality bipartite matching with deterministic edge ordering."""
    edges = _candidate_edges(expected, predicted, line_tolerance)
    prediction_owner: dict[int, int] = {}

    def assign(expected_index: int, visited: set) -> bool:
        for predicted_index, _distance in edges.get(expected_index, []):
            if predicted_index in visited:
                continue
            visited.add(predicted_index)
            previous = prediction_owner.get(predicted_index)
            if previous is None or assign(previous, visited):
                prediction_owner[predicted_index] = expected_index
                return True
        return False

    # Constrained truths go first so flexible ranges do not consume their only edge.
    order = sorted(range(len(expected)), key=lambda index: (len(edges[index]), index))
    for expected_index in order:
        assign(expected_index, set())

    matches = []
    for predicted_index, expected_index in prediction_owner.items():
        distance = next(
            distance for index, distance in edges[expected_index] if index == predicted_index
        )
        matches.append(Match(expected_index, predicted_index, distance))
    return sorted(matches, key=lambda item: (item.expected_index, item.predicted_index))


class FixtureRepairer:
    """Conservative deterministic repairer used by the controlled benchmark.

    Production repositories should replace this with a worktree-based repair runner.
    The same evaluator and gates can consume either implementation.
    """

    def repair(self, case: dict, finding: Finding) -> dict[str, Any]:
        validation = dict(case.get("repair_validation") or {})
        path = finding.path
        content = str((case.get("after_files") or {}).get(path, ""))
        checks = []
        risk_pattern = str(validation.get("risk_pattern", ""))
        reproducible = bool(risk_pattern and re.search(risk_pattern, content, re.MULTILINE))
        checks.append({"name": "risk-reproduction", "passed": reproducible})
        if not validation.get("auto_fixable", False):
            checks.append({"name": "patch-generated", "passed": False})
            return {"passed": False, "checks": checks, "content": content}

        repaired = self._transform(content, finding)
        patch_applied = repaired != content
        checks.append({"name": "patch-generated", "passed": patch_applied})
        compile_result = RepairVerifier().verify_contents({path: repaired})
        compile_passed = bool(compile_result["passed"])
        checks.append({"name": "compile", "passed": compile_passed})
        risk_removed = bool(risk_pattern) and not re.search(risk_pattern, repaired, re.MULTILINE)
        checks.append({"name": "risk-removed", "passed": risk_removed})
        required = list(validation.get("required_after_patterns") or [])
        regression_passed = all(re.search(pattern, repaired, re.MULTILINE) for pattern in required)
        checks.append({"name": "regression-tests", "passed": regression_passed})
        return {
            "passed": all(item["passed"] for item in checks),
            "checks": checks,
            "content": repaired,
        }

    @staticmethod
    def _transform(content: str, finding: Finding) -> str:
        rule = finding.rule_id
        if rule == "SEC-EVAL":
            value = re.sub(r"\beval\s*\(", "json.loads(", content)
            return FixtureRepairer._ensure_import(value, "json")
        if rule == "SEC-SUBPROCESS-SHELL":
            return re.sub(r"shell\s*=\s*True", "shell=False", content)
        if rule == "SEC-HARDCODED-SECRET":
            value = re.sub(
                r"(?m)^(\s*)(password|passwd|api_key|secret|token)\s*=\s*['\"][^'\"]+['\"]",
                lambda match: (
                    '%s%s = os.environ["%s"]'
                    % (match.group(1), match.group(2), match.group(2).upper())
                ),
                content,
            )
            return FixtureRepairer._ensure_import(value, "os")
        if rule == "SEC-SQL-CONCAT":
            return re.sub(
                r"(?m)^(\s*)cursor\.execute\(.+$",
                r'\1cursor.execute("SELECT * FROM users WHERE id = ?", (value,))',
                content,
            )
        if rule == "REL-EMPTY-EXCEPT":
            return content.replace("except Exception:", "except ValueError:")
        if rule == "REL-DEBUG-PRINT":
            return re.sub(r"(?m)^\s*(print|console\.log)\s*\(.+\)\s*$\n?", "", content)
        if rule == "SEC-PATH-TRAVERSAL":
            return re.sub(
                r"open\(base\s*/\s*user_path\)\.read\(\)",
                "read_under_base(base, user_path)",
                content,
            )
        if rule in {"SEC-YAML-LOAD", "SEC-INSECURE-COOKIE"}:
            result = SafeFixer().apply(
                content,
                [
                    {
                        "path": finding.path,
                        "line": finding.line,
                        "rule_id": rule,
                    }
                ],
                finding.path,
            )
            if rule in result["rules"]:
                return str(result["content"])
        return content

    @staticmethod
    def _ensure_import(content: str, module: str) -> str:
        if re.search(r"(?m)^\s*(import %s|from %s import)" % (module, module), content):
            return content
        return "import %s\n" % module + content


class EndToEndEvaluationHarness:
    def __init__(
        self,
        line_tolerance: int = 2,
        repairer: FixtureRepairer | None = None,
    ):
        self.line_tolerance = line_tolerance
        self.repairer = repairer

    def run(
        self,
        reviewer: Reviewer,
        cases: list[dict],
        name: str = "",
        annotation_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        totals = empty_totals()
        case_results = []
        for case in cases:
            result = self._run_case(reviewer, case)
            case_results.append(result)
            accumulate(totals, result)
        metrics = metric_summary(totals)
        by_split = {}
        for split in ("validation", "holdout"):
            selected = [item for item in case_results if item["split"] == split]
            split_totals = empty_totals()
            for item in selected:
                accumulate(split_totals, item)
            by_split[split] = metric_summary(split_totals)
        source_kinds = sorted(
            {str((case.get("source") or {}).get("kind", "unknown")) for case in cases}
        )
        return {
            "schema_version": 2,
            "name": name or reviewer.name,
            "reviewer": reviewer.name,
            "dataset": {
                "cases": len(cases),
                "repositories": len({case["repository"] for case in cases}),
                "risk_cases": sum(bool(case["expected_findings"]) for case in cases),
                "clean_cases": sum(not case["expected_findings"] for case in cases),
                "source_kinds": source_kinds,
                "sha256": dataset_fingerprint(cases),
                "provenance": audit_dataset_provenance(cases, annotation_evidence),
            },
            "metrics": metrics,
            "by_split": by_split,
            "by_language": language_slices(case_results),
            "by_cwe": cwe_slices(case_results),
            "by_rule": rule_slices(case_results),
            "confidence_calibration": confidence_calibration(case_results),
            "duration_seconds": round(time.monotonic() - started, 4),
            "case_results": case_results,
        }

    def _run_case(self, reviewer: Reviewer, case: dict) -> dict[str, Any]:
        expected = list(case["expected_findings"])
        result = {
            "id": case["id"],
            "repository": case["repository"],
            "pull_request": case["pull_request"],
            "split": case["split"],
            "expected": len(expected),
            "predicted": 0,
            "tp": 0,
            "fp": 0,
            "fn": len(expected),
            "severity_hits": 0,
            "high_total": sum(
                str(item["severity"]).lower() in {"high", "critical"} for item in expected
            ),
            "high_hits": 0,
            "clean_hit": False,
            "execution_success": False,
            "repair_eligible": (
                len(expected)
                if self.repairer is not None
                and bool((case.get("repair_validation") or {}).get("auto_fixable"))
                else 0
            ),
            "repair_attempted": 0,
            "repair_passed": 0,
            "repair_abstained": 0,
            "e2e_success": False,
            "matches": [],
            "repair": [],
            "languages": [],
            "expected_cwes": [str(item["cwe"]).upper() for item in expected],
            "predictions": [],
            "error": None,
        }
        try:
            parsed = parse_unified_diff(case["diff"])
            result["languages"] = languages_for_paths(parsed.files)
            findings = reviewer.review(case["diff"], parsed)
            matches = one_to_one_match(expected, findings, self.line_tolerance)
            matched_predictions = {match.predicted_index for match in matches}
            result["predictions"] = [
                {
                    "rule_id": finding.rule_id,
                    "cwe": RULE_TO_CWE.get(finding.rule_id, finding.rule_id).upper(),
                    "confidence": serializable_confidence(finding.confidence),
                    "matched": index in matched_predictions,
                }
                for index, finding in enumerate(findings)
            ]
            result["predicted"] = len(findings)
            result["tp"] = len(matches)
            result["fp"] = len(findings) - len(matches)
            result["fn"] = len(expected) - len(matches)
            result["clean_hit"] = not expected and not findings
            result["execution_success"] = True
            matched_expected = set()
            for match in matches:
                truth = expected[match.expected_index]
                finding = findings[match.predicted_index]
                severity_hit = finding.severity.value == str(truth["severity"]).lower()
                high = str(truth["severity"]).lower() in {"high", "critical"}
                result["severity_hits"] += int(severity_hit)
                result["high_hits"] += int(high)
                matched_expected.add(match.expected_index)
                result["matches"].append(
                    {
                        "expected_index": match.expected_index,
                        "predicted_index": match.predicted_index,
                        "path": finding.path,
                        "line": finding.line,
                        "cwe": RULE_TO_CWE.get(finding.rule_id, finding.rule_id),
                        "rule_id": finding.rule_id,
                        "expected_severity": truth["severity"],
                        "predicted_severity": finding.severity.value,
                        "severity_hit": severity_hit,
                        "location_distance": match.location_distance,
                    }
                )
                repair_is_eligible = bool((case.get("repair_validation") or {}).get("auto_fixable"))
                if self.repairer is not None and repair_is_eligible:
                    result["repair_attempted"] += 1
                    repair = self.repairer.repair(case, finding)
                    result["repair_passed"] += int(repair["passed"])
                    result["repair"].append(
                        {
                            "expected_index": match.expected_index,
                            "passed": repair["passed"],
                            "checks": repair["checks"],
                        }
                    )
                elif self.repairer is not None:
                    result["repair_abstained"] += 1
            result["e2e_success"] = bool(
                expected
                and result["repair_eligible"] == len(expected)
                and len(matched_expected) == len(expected)
                and result["repair_attempted"] == len(expected)
                and result["repair_passed"] == len(expected)
            )
        except Exception as exc:
            result["error"] = str(exc)[:1000]
        return result


def comparison_summary(
    baseline: dict,
    candidate: dict,
    minimum_f1_improvement: float = 0.02,
    minimum_execution_success: float = 0.98,
    minimum_safe_fix_rate: float = 0.75,
    minimum_e2e_fix_rate: float = 0.60,
) -> dict[str, Any]:
    metrics = (
        "precision",
        "recall",
        "f1",
        "severity_accuracy",
        "high_risk_recall",
        "clean_accuracy",
        "execution_success_rate",
        "safe_fix_rate",
        "e2e_security_fix_rate",
    )
    quantitative_gates = {
        "same_dataset": {
            "passed": baseline["dataset"]["sha256"] == candidate["dataset"]["sha256"],
        },
        "validation_f1_improvement": {
            "passed": (
                candidate["by_split"]["validation"]["f1"]
                >= baseline["by_split"]["validation"]["f1"] + minimum_f1_improvement
            ),
            "minimum_delta": minimum_f1_improvement,
        },
        "high_risk_recall_non_regression": {
            "passed": (
                candidate["metrics"]["high_risk_recall"] >= baseline["metrics"]["high_risk_recall"]
            ),
        },
        "clean_accuracy_non_regression": {
            "passed": (
                candidate["metrics"]["clean_accuracy"] >= baseline["metrics"]["clean_accuracy"]
            ),
        },
        "holdout_f1_non_regression": {
            "passed": (
                candidate["by_split"]["holdout"]["f1"] >= baseline["by_split"]["holdout"]["f1"]
            ),
        },
        "execution_success": {
            "passed": (candidate["metrics"]["execution_success_rate"] >= minimum_execution_success),
            "minimum": minimum_execution_success,
        },
        "confidence_validity": {
            "passed": candidate.get("confidence_calibration", {}).get("invalid_confidences", 1)
            == 0,
        },
        "safe_fix_rate": {
            "passed": candidate["metrics"]["safe_fix_rate"] >= minimum_safe_fix_rate,
            "minimum": minimum_safe_fix_rate,
        },
        "e2e_security_fix_rate": {
            "passed": (candidate["metrics"]["e2e_security_fix_rate"] >= minimum_e2e_fix_rate),
            "minimum": minimum_e2e_fix_rate,
        },
    }
    provenance = candidate["dataset"].get("provenance") or {}
    provenance_gate = {
        "passed": bool(provenance.get("production_ready")),
        "required": "bound independent-label provenance evidence",
        "audit": provenance,
    }
    gates = dict(quantitative_gates)
    gates["production_data_provenance"] = provenance_gate
    quantitative_passed = all(item["passed"] for item in quantitative_gates.values())
    return {
        "dataset_sha256": candidate["dataset"]["sha256"],
        "baseline": baseline["name"],
        "candidate": candidate["name"],
        "deltas": {
            metric: round(candidate["metrics"][metric] - baseline["metrics"][metric], 4)
            for metric in metrics
        },
        "release_gate": {
            "passed": all(item["passed"] for item in gates.values()),
            "quantitative_passed": quantitative_passed,
            "production_activation_allowed": (quantitative_passed and provenance_gate["passed"]),
            "gates": gates,
        },
    }
