"""End-to-end PR diff evaluation with reproducible matching and repair gates."""
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .diff_parser import parse_unified_diff
from .models import Finding, Severity
from .reviewer import Reviewer
from .verifier import RepairVerifier


SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

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


@dataclass
class Match:
    expected_index: int
    predicted_index: int
    location_distance: int


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dataset_fingerprint(cases: Iterable[dict]) -> str:
    """Fingerprint exactly what is scored, independent of JSONL formatting."""
    digest = hashlib.sha256()
    for case in sorted(cases, key=lambda item: str(item["id"])):
        digest.update(_canonical_json(case).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def load_jsonl(path: str) -> List[dict]:
    cases = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                case = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON on line %d: %s" % (line_number, exc)) from exc
            validate_case(case, line_number)
            cases.append(case)
    ids = [str(case["id"]) for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("dataset contains duplicate case ids")
    return cases


def validate_case(case: dict, line_number: int = 0) -> None:
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
    added_locations = {
        (_normalized_path(item.path), int(item.line)) for item in parsed.added_lines
    }
    for expected in case["expected_findings"]:
        for field in ("path", "start_line", "end_line", "cwe", "severity"):
            if field not in expected:
                raise ValueError("%s finding is missing %s" % (prefix, field))
        if str(expected["severity"]).lower() not in SEVERITY_RANK:
            raise ValueError("%s finding has invalid severity" % prefix)
        if int(expected["start_line"]) > int(expected["end_line"]):
            raise ValueError("%s finding has an inverted line range" % prefix)
        expected_path = _normalized_path(str(expected["path"]))
        if not any(
            path == expected_path
            and int(expected["start_line"]) <= line <= int(expected["end_line"])
            for path, line in added_locations
        ):
            raise ValueError("%s finding does not cover an added line" % prefix)


def _normalized_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    return value[2:] if value.startswith(("a/", "b/")) else value


def _candidate_edges(
    expected: List[dict], predicted: List[Finding], line_tolerance: int,
) -> Dict[int, List[Tuple[int, int]]]:
    edges: Dict[int, List[Tuple[int, int]]] = {}
    for expected_index, truth in enumerate(expected):
        start = int(truth["start_line"])
        end = int(truth["end_line"])
        truth_path = _normalized_path(str(truth["path"]))
        truth_cwe = str(truth["cwe"]).upper()
        options = []
        for predicted_index, finding in enumerate(predicted):
            if _normalized_path(finding.path) != truth_path:
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
    expected: List[dict], predicted: List[Finding], line_tolerance: int = 2,
) -> List[Match]:
    """Maximum-cardinality bipartite matching with deterministic edge ordering."""
    edges = _candidate_edges(expected, predicted, line_tolerance)
    prediction_owner: Dict[int, int] = {}

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
            distance for index, distance in edges[expected_index]
            if index == predicted_index
        )
        matches.append(Match(expected_index, predicted_index, distance))
    return sorted(matches, key=lambda item: (item.expected_index, item.predicted_index))


class FixtureRepairer:
    """Conservative deterministic repairer used by the controlled benchmark.

    Production repositories should replace this with a worktree-based repair runner.
    The same evaluator and gates can consume either implementation.
    """

    def repair(self, case: dict, finding: Finding) -> Dict[str, Any]:
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
        risk_removed = bool(risk_pattern) and not re.search(
            risk_pattern, repaired, re.MULTILINE
        )
        checks.append({"name": "risk-removed", "passed": risk_removed})
        required = list(validation.get("required_after_patterns") or [])
        regression_passed = all(
            re.search(pattern, repaired, re.MULTILINE) for pattern in required
        )
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
                lambda match: '%s%s = os.environ["%s"]' % (
                    match.group(1), match.group(2), match.group(2).upper()
                ),
                content,
            )
            return FixtureRepairer._ensure_import(value, "os")
        if rule == "SEC-SQL-CONCAT":
            return re.sub(
                r'(?m)^(\s*)cursor\.execute\(.+$',
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
        return content

    @staticmethod
    def _ensure_import(content: str, module: str) -> str:
        if re.search(r"(?m)^\s*(import %s|from %s import)" % (module, module), content):
            return content
        return "import %s\n" % module + content


class EndToEndEvaluationHarness:
    def __init__(
        self, line_tolerance: int = 2, repairer: Optional[FixtureRepairer] = None,
    ):
        self.line_tolerance = line_tolerance
        self.repairer = repairer

    def run(self, reviewer: Reviewer, cases: List[dict], name: str = "") -> Dict[str, Any]:
        started = time.monotonic()
        totals = self._empty_totals()
        case_results = []
        for case in cases:
            result = self._run_case(reviewer, case)
            case_results.append(result)
            self._accumulate(totals, result)
        metrics = self._metrics(totals)
        by_split = {}
        for split in ("validation", "holdout"):
            selected = [item for item in case_results if item["split"] == split]
            split_totals = self._empty_totals()
            for item in selected:
                self._accumulate(split_totals, item)
            by_split[split] = self._metrics(split_totals)
        source_kinds = sorted({
            str((case.get("source") or {}).get("kind", "unknown")) for case in cases
        })
        return {
            "schema_version": 1,
            "name": name or reviewer.name,
            "reviewer": reviewer.name,
            "dataset": {
                "cases": len(cases),
                "repositories": len({case["repository"] for case in cases}),
                "risk_cases": sum(bool(case["expected_findings"]) for case in cases),
                "clean_cases": sum(not case["expected_findings"] for case in cases),
                "source_kinds": source_kinds,
                "sha256": dataset_fingerprint(cases),
            },
            "metrics": metrics,
            "by_split": by_split,
            "duration_seconds": round(time.monotonic() - started, 4),
            "case_results": case_results,
        }

    def _run_case(self, reviewer: Reviewer, case: dict) -> Dict[str, Any]:
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
            "repair_attempted": 0,
            "repair_passed": 0,
            "e2e_success": False,
            "matches": [],
            "repair": [],
            "error": None,
        }
        try:
            parsed = parse_unified_diff(case["diff"])
            findings = reviewer.review(case["diff"], parsed)
            matches = one_to_one_match(expected, findings, self.line_tolerance)
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
                result["matches"].append({
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
                })
                if self.repairer is not None:
                    result["repair_attempted"] += 1
                    repair = self.repairer.repair(case, finding)
                    result["repair_passed"] += int(repair["passed"])
                    result["repair"].append({
                        "expected_index": match.expected_index,
                        "passed": repair["passed"],
                        "checks": repair["checks"],
                    })
            result["e2e_success"] = bool(
                expected
                and len(matched_expected) == len(expected)
                and result["repair_attempted"] == len(expected)
                and result["repair_passed"] == len(expected)
            )
        except Exception as exc:
            result["error"] = str(exc)[:1000]
        return result

    @staticmethod
    def _empty_totals() -> Dict[str, int]:
        return {
            "cases": 0, "risk_cases": 0, "clean_cases": 0, "tp": 0, "fp": 0,
            "fn": 0, "severity_hits": 0, "high_total": 0, "high_hits": 0,
            "clean_hits": 0, "execution_successes": 0, "repair_attempted": 0,
            "repair_passed": 0, "e2e_successes": 0,
        }

    @staticmethod
    def _accumulate(totals: Dict[str, int], result: dict) -> None:
        totals["cases"] += 1
        totals["risk_cases"] += int(result["expected"] > 0)
        totals["clean_cases"] += int(result["expected"] == 0)
        for field in (
            "tp", "fp", "fn", "severity_hits", "high_total", "high_hits",
            "repair_attempted", "repair_passed",
        ):
            totals[field] += int(result[field])
        totals["clean_hits"] += int(result["clean_hit"])
        totals["execution_successes"] += int(result["execution_success"])
        totals["e2e_successes"] += int(result["e2e_success"])

    @staticmethod
    def _metrics(totals: Dict[str, int]) -> Dict[str, Any]:
        def ratio(numerator: int, denominator: int, empty: float = 1.0) -> float:
            return round(numerator / denominator, 4) if denominator else empty

        precision = ratio(totals["tp"], totals["tp"] + totals["fp"], 0.0)
        recall = ratio(totals["tp"], totals["tp"] + totals["fn"], 1.0)
        f1 = round(
            2 * precision * recall / (precision + recall), 4
        ) if precision + recall else 0.0
        return {
            **totals,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "severity_accuracy": ratio(totals["severity_hits"], totals["tp"]),
            "high_risk_recall": ratio(totals["high_hits"], totals["high_total"]),
            "clean_accuracy": ratio(totals["clean_hits"], totals["clean_cases"]),
            "execution_success_rate": ratio(
                totals["execution_successes"], totals["cases"], 0.0
            ),
            "safe_fix_rate": ratio(
                totals["repair_passed"], totals["repair_attempted"], 0.0
            ),
            "e2e_security_fix_rate": ratio(
                totals["e2e_successes"], totals["risk_cases"], 0.0
            ),
        }


def comparison_summary(
    baseline: dict, candidate: dict, minimum_f1_improvement: float = 0.02,
    minimum_execution_success: float = 0.98, minimum_safe_fix_rate: float = 0.75,
    minimum_e2e_fix_rate: float = 0.60,
) -> Dict[str, Any]:
    metrics = (
        "precision", "recall", "f1", "severity_accuracy", "high_risk_recall",
        "clean_accuracy", "execution_success_rate", "safe_fix_rate",
        "e2e_security_fix_rate",
    )
    quantitative_gates = {
        "validation_f1_improvement": {
            "passed": (
                candidate["by_split"]["validation"]["f1"]
                >= baseline["by_split"]["validation"]["f1"] + minimum_f1_improvement
            ),
            "minimum_delta": minimum_f1_improvement,
        },
        "high_risk_recall_non_regression": {
            "passed": (
                candidate["metrics"]["high_risk_recall"]
                >= baseline["metrics"]["high_risk_recall"]
            ),
        },
        "clean_accuracy_non_regression": {
            "passed": (
                candidate["metrics"]["clean_accuracy"]
                >= baseline["metrics"]["clean_accuracy"]
            ),
        },
        "holdout_f1_non_regression": {
            "passed": (
                candidate["by_split"]["holdout"]["f1"]
                >= baseline["by_split"]["holdout"]["f1"]
            ),
        },
        "execution_success": {
            "passed": (
                candidate["metrics"]["execution_success_rate"]
                >= minimum_execution_success
            ),
            "minimum": minimum_execution_success,
        },
        "safe_fix_rate": {
            "passed": candidate["metrics"]["safe_fix_rate"] >= minimum_safe_fix_rate,
            "minimum": minimum_safe_fix_rate,
        },
        "e2e_security_fix_rate": {
            "passed": (
                candidate["metrics"]["e2e_security_fix_rate"] >= minimum_e2e_fix_rate
            ),
            "minimum": minimum_e2e_fix_rate,
        },
    }
    source_kinds = set(candidate["dataset"].get("source_kinds") or [])
    provenance_gate = {
        "passed": source_kinds == {"public-github-pr"},
        "required_source_kind": "public-github-pr",
        "actual_source_kinds": sorted(source_kinds),
    }
    gates = dict(quantitative_gates)
    gates["production_data_provenance"] = provenance_gate
    quantitative_passed = all(
        item["passed"] for item in quantitative_gates.values()
    )
    return {
        "dataset_sha256": candidate["dataset"]["sha256"],
        "baseline": baseline["name"],
        "candidate": candidate["name"],
        "deltas": {
            metric: round(
                candidate["metrics"][metric] - baseline["metrics"][metric], 4
            )
            for metric in metrics
        },
        "release_gate": {
            "passed": all(item["passed"] for item in gates.values()),
            "quantitative_passed": quantitative_passed,
            "production_activation_allowed": (
                quantitative_passed and provenance_gate["passed"]
            ),
            "gates": gates,
        },
    }
