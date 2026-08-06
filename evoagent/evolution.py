import hashlib
import json
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from .diff_parser import parse_unified_diff
from .store import utc_now


DEFAULT_PROMPT = (
    "Review the unified diff. Return JSON findings with severity, fix and test. "
    "Report only actionable defects introduced by added lines."
)

DEFAULT_EVALUATION_CASES = [
    {
        "name": "security-dynamic-execution",
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+result = eval(user_input)\n",
        "expected": [{"path": "app.py", "line": 1, "min_severity": "high"}],
    },
    {
        "name": "security-hardcoded-secret",
        "diff": "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+api_key = \"production-secret\"\n",
        "expected": [{"path": "config.py", "line": 1, "min_severity": "high"}],
    },
    {
        "name": "reliability-swallowed-error",
        "diff": "--- a/jobs.py\n+++ b/jobs.py\n@@ -1 +1 @@\n-old\n+except Exception: pass\n",
        "expected": [{"path": "jobs.py", "line": 1, "min_severity": "medium"}],
    },
    {
        "name": "clean-parameterized-query",
        "diff": "--- a/db.py\n+++ b/db.py\n@@ -1 +1 @@\n-old\n+cursor.execute(\"SELECT * FROM users WHERE id = ?\", (user_id,))\n",
        "expected": [],
    },
    {
        "name": "clean-input-validation",
        "diff": "--- a/api.py\n+++ b/api.py\n@@ -1 +1 @@\n-old\n+limit = min(max(int(raw_limit), 1), 100)\n",
        "expected": [],
    },
    {
        "name": "holdout-security-shell-execution",
        "split": "holdout",
        "diff": "--- a/runner.py\n+++ b/runner.py\n@@ -1 +1 @@\n-old\n+subprocess.run(command, shell=True)\n",
        "expected": [{"path": "runner.py", "line": 1, "min_severity": "high"}],
    },
    {
        "name": "holdout-clean-environment-secret",
        "split": "holdout",
        "diff": "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+api_key = os.environ[\"API_KEY\"]\n",
        "expected": [],
    },
]

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class RegressionEvaluator:
    """Replay a fixed dataset against one prompt and compute objective review metrics."""

    def __init__(self, reviewer_factory: Callable[[str], object]):
        self.reviewer_factory = reviewer_factory

    def run(self, prompt: str, cases: List[dict]) -> Dict[str, Any]:
        reviewer = self.reviewer_factory(prompt)
        reviewer_name = str(getattr(reviewer, "name", reviewer.__class__.__name__))
        true_positive = false_positive = false_negative = 0
        severity_hits = matched = clean_hits = clean_total = 0
        high_severity_hits = high_severity_total = 0
        expected_total = predicted_total = 0
        errors = []
        case_results = []

        for case in cases:
            expected_items = list(case.get("expected", []))
            expected_total += len(expected_items)
            clean_total += int(not expected_items)
            try:
                parsed = parse_unified_diff(case["diff"])
                findings = reviewer.review(case["diff"], parsed)
                predicted = {}
                for finding in findings:
                    key = (finding.path, int(finding.line), finding.rule_id)
                    current = predicted.get(key)
                    severity = finding.severity.value
                    if current is None or SEVERITY_RANK[severity] > SEVERITY_RANK[current]:
                        predicted[key] = severity
                predicted_total += len(predicted)

                unmatched = set(predicted)
                tp = severity_ok = high_hits = 0
                for expected in expected_items:
                    path = str(expected["path"])
                    line = int(expected["line"])
                    rule_id = str(expected.get("rule_id", "")).strip()
                    minimum = str(expected.get("min_severity", "low")).lower()
                    candidates = [
                        key for key in unmatched
                        if key[0] == path and key[1] == line and (not rule_id or key[2] == rule_id)
                    ]
                    if not candidates:
                        continue
                    selected = max(candidates, key=lambda key: SEVERITY_RANK[predicted[key]])
                    unmatched.remove(selected)
                    tp += 1
                    severity_passed = SEVERITY_RANK[predicted[selected]] >= SEVERITY_RANK[minimum]
                    severity_ok += int(severity_passed)
                    if SEVERITY_RANK[minimum] >= SEVERITY_RANK["high"]:
                        high_hits += int(severity_passed)

                fp = len(unmatched)
                fn = len(expected_items) - tp
                true_positive += tp
                false_positive += fp
                false_negative += fn
                matched += tp
                severity_hits += severity_ok
                case_high_total = sum(
                    SEVERITY_RANK[str(item.get("min_severity", "low")).lower()]
                    >= SEVERITY_RANK["high"]
                    for item in expected_items
                )
                high_severity_total += case_high_total
                high_severity_hits += high_hits
                if not expected_items:
                    clean_hits += int(not predicted)
                case_results.append({
                    "id": case.get("id"), "name": case["name"], "tp": tp, "fp": fp, "fn": fn,
                    "findings": len(predicted), "severity_hits": severity_ok, "error": None,
                })
            except Exception as exc:
                # A failed replay is a missed positive (or a failed clean case), not a
                # successful empty prediction. This keeps partial outages from inflating scores.
                false_negative += len(expected_items)
                high_severity_total += sum(
                    SEVERITY_RANK.get(str(item.get("min_severity", "low")).lower(), 0)
                    >= SEVERITY_RANK["high"]
                    for item in expected_items
                )
                errors.append({"name": case["name"], "error": str(exc)[:500]})
                case_results.append({
                    "id": case.get("id"), "name": case["name"], "tp": 0, "fp": 0,
                    "fn": len(expected_items), "findings": 0, "severity_hits": 0,
                    "error": str(exc)[:500],
                })

        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive else (1.0 if true_positive + false_negative == 0 else 0.0)
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative else 1.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        severity_accuracy = severity_hits / matched if matched else (1.0 if not false_negative else 0.0)
        clean_accuracy = clean_hits / clean_total if clean_total else 1.0
        high_severity_recall = (
            high_severity_hits / high_severity_total if high_severity_total else 1.0
        )
        successful_cases = len(cases) - len(errors)
        success_rate = successful_cases / len(cases) if cases else 0.0
        components = []
        if expected_total:
            components.extend(((f1, 0.65), (severity_accuracy, 0.15)))
        if clean_total:
            components.append((clean_accuracy, 0.20))
        score = (
            sum(value * weight for value, weight in components)
            / sum(weight for _, weight in components)
            if components else 0.0
        )
        score *= success_rate
        return {
            "schema_version": 2,
            "reviewer": reviewer_name,
            "score": round(score, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "severity_accuracy": round(severity_accuracy, 4),
            "high_severity_recall": round(high_severity_recall, 4),
            "clean_accuracy": round(clean_accuracy, 4),
            "cases": len(cases),
            "positive_cases": sum(bool(case.get("expected")) for case in cases),
            "clean_cases": clean_total,
            "expected_findings": expected_total,
            "predicted_findings": predicted_total,
            "successful_cases": successful_cases,
            "success_rate": round(success_rate, 4),
            "errors": errors,
            "case_results": case_results,
        }


class EvolutionEngine:
    """Prompt evolution backed by replay evaluation, audit records and activation gates."""

    FORBIDDEN = ("ignore previous", "disable safety", "bypass", "直接执行生产")

    def __init__(
        self, store, reviewer_factory: Optional[Callable[[str], object]] = None,
        min_cases: int = 3, max_cases: int = 5, min_improvement: float = 0.01,
        min_holdout_cases: int = 0, max_metric_regression: float = 0.0,
        seed_defaults: bool = True,
    ):
        self.store = store
        self.reviewer_factory = reviewer_factory
        self.min_cases = min_cases
        self.max_cases = max_cases
        self.min_improvement = min_improvement
        self.min_holdout_cases = min_holdout_cases
        self.max_metric_regression = max_metric_regression
        self._lock = threading.RLock()
        if seed_defaults:
            self._seed_default_cases()

    def _seed_default_cases(self) -> None:
        for case in DEFAULT_EVALUATION_CASES:
            self.store.save_evaluation_case(
                case["name"], case.get("split", "validation"), case["diff"],
                case["expected"], "builtin", True
            )

    @staticmethod
    def validate_case(name: str, diff: str, expected: list, split: str = "validation") -> None:
        if not name or len(name) > 120:
            raise ValueError("evaluation case name is required and must be at most 120 characters")
        if split not in {"train", "validation", "holdout"}:
            raise ValueError("evaluation split must be train, validation or holdout")
        if len(diff.encode("utf-8")) > 1024 * 1024:
            raise ValueError("evaluation case diff must be at most 1 MiB")
        parsed = parse_unified_diff(diff)
        if not parsed.added_lines:
            raise ValueError("evaluation case must contain a valid unified diff with added lines")
        valid_locations = {(line.path, line.line) for line in parsed.added_lines}
        if not isinstance(expected, list):
            raise ValueError("expected_findings must be an array")
        if len(expected) > 100:
            raise ValueError("expected_findings must contain at most 100 items")
        seen = set()
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError("each expected finding must be an object")
            try:
                location = (str(item.get("path", "")), int(item.get("line", 0)))
            except (TypeError, ValueError) as exc:
                raise ValueError("expected finding line must be an integer") from exc
            if location not in valid_locations:
                raise ValueError("expected finding must point to an added line: %s:%s" % location)
            if str(item.get("min_severity", "low")).lower() not in SEVERITY_RANK:
                raise ValueError("invalid min_severity")
            rule_id = str(item.get("rule_id", "")).strip()
            if "rule_id" in item and (not rule_id or len(rule_id) > 80):
                raise ValueError("rule_id must be non-empty and at most 80 characters")
            identity = location + (rule_id,)
            if identity in seen:
                raise ValueError("duplicate expected finding: %s:%s:%s" % identity)
            seen.add(identity)

    def add_evaluation_case(
        self, name: str, diff: str, expected: list, split: str = "validation",
        source: str = "manual",
    ) -> dict:
        name = name.strip()
        split = split.strip().lower()
        self.validate_case(name, diff, expected, split)
        return self.store.save_evaluation_case(name, split, diff, expected, source[:120], True)

    def safety_evaluate(self, prompt: str) -> Dict[str, Any]:
        normalized = prompt.strip()
        lowered = normalized.lower()
        safety = bool(normalized) and len(normalized) <= 12000 and not any(
            token in lowered for token in self.FORBIDDEN
        )
        required = ("diff", "severity", "fix", "test", "json")
        completeness = sum(token in lowered for token in required) / len(required)
        return {
            "safety_passed": safety,
            "completeness": round(completeness, 4),
            "missing_terms": [token for token in required if token not in lowered],
        }

    def status(self) -> Dict[str, Any]:
        cases = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout = self.store.list_evaluation_cases("holdout", True, self.max_cases)
        return {
            "model_configured": self.reviewer_factory is not None,
            "validation_cases": len(cases),
            "holdout_cases": len(holdout),
            "minimum_cases": self.min_cases,
            "minimum_holdout_cases": self.min_holdout_cases,
            "maximum_cases_per_run": self.max_cases,
            "minimum_improvement": self.min_improvement,
            "maximum_metric_regression": self.max_metric_regression,
            "validation_dataset_fingerprint": self._dataset_fingerprint(cases),
            "holdout_dataset_fingerprint": self._dataset_fingerprint(holdout),
            "ready": (
                self.reviewer_factory is not None
                and len(cases) >= self.min_cases
                and len(holdout) >= self.min_holdout_cases
            ),
        }

    def propose(
        self, skill_name: str, prompt: str, regression_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        skill_name = skill_name.strip()
        if not skill_name or len(skill_name) > 120:
            raise ValueError("skill_name is required and must be at most 120 characters")
        with self._lock:
            return self._propose(skill_name, prompt, regression_score)

    def _propose(
        self, skill_name: str, prompt: str, regression_score: Optional[float],
    ) -> Dict[str, Any]:
        safety = self.safety_evaluate(prompt)
        active = self.store.get_active_skill_version(skill_name)
        if active and prompt.strip() == active["prompt"].strip():
            return {
                "version": {
                    "skill_name": active["skill_name"],
                    "version": active["version"],
                    "score": active["score"],
                    "active": bool(active["active"]),
                },
                "decision": "deferred",
                "reason": "candidate prompt is identical to the active version",
                "candidate": self._empty_metrics(0),
                "baseline": self._empty_metrics(0),
                "candidate_holdout": self._redact_holdout_metrics(self._empty_metrics(0)),
                "baseline_holdout": self._redact_holdout_metrics(self._empty_metrics(0)),
                "safety": safety,
                "run_id": None,
            }
        baseline_prompt = active["prompt"] if active else DEFAULT_PROMPT
        cases = self.store.list_evaluation_cases("validation", True, self.max_cases)
        holdout_cases = self.store.list_evaluation_cases("holdout", True, self.max_cases)

        decision = "deferred"
        reason = ""
        candidate_metrics = self._empty_metrics(len(cases))
        baseline_metrics = self._empty_metrics(len(cases))
        candidate_holdout = self._empty_metrics(len(holdout_cases))
        baseline_holdout = self._empty_metrics(len(holdout_cases))
        gates = {
            "safety": safety["safety_passed"] and safety["completeness"] == 1.0,
            "validation_dataset_ready": len(cases) >= self.min_cases,
            "holdout_dataset_ready": len(holdout_cases) >= self.min_holdout_cases,
            "evaluation_success": None,
            "validation_improvement": None,
            "validation_non_regression": None,
            "holdout_non_regression": None,
        }
        if not safety["safety_passed"] or safety["completeness"] < 1.0:
            decision = "rejected"
            reason = "candidate prompt failed the deterministic safety/completeness gate"
        elif self.reviewer_factory is None:
            reason = "candidate saved but no LLM provider is configured; replay evaluation was not run"
        elif len(cases) < self.min_cases:
            reason = "candidate saved but the validation dataset is smaller than the activation minimum"
        elif len(holdout_cases) < self.min_holdout_cases:
            reason = "candidate saved but the holdout dataset is smaller than the activation minimum"
        else:
            evaluator = RegressionEvaluator(self.reviewer_factory)
            baseline_metrics = evaluator.run(baseline_prompt, cases)
            candidate_metrics = evaluator.run(prompt, cases)
            if holdout_cases:
                baseline_holdout = evaluator.run(baseline_prompt, holdout_cases)
                candidate_holdout = evaluator.run(prompt, holdout_cases)
            no_errors = not (
                baseline_metrics["errors"] or candidate_metrics["errors"]
                or baseline_holdout["errors"] or candidate_holdout["errors"]
            )
            improved = candidate_metrics["score"] >= baseline_metrics["score"] + self.min_improvement
            validation_safe = self._non_regressing(candidate_metrics, baseline_metrics)
            holdout_safe = self._non_regressing(candidate_holdout, baseline_holdout)
            gates.update({
                "evaluation_success": no_errors,
                "validation_improvement": improved,
                "validation_non_regression": validation_safe,
                "holdout_non_regression": holdout_safe,
            })
            if no_errors and improved and validation_safe and holdout_safe:
                decision = "activated"
                reason = "candidate improved on validation and passed the non-regression holdout gate"
            else:
                decision = "rejected"
                reasons = []
                if not no_errors:
                    reasons.append("one or more baseline or candidate evaluations failed")
                if not improved:
                    reasons.append("score improvement was below the configured threshold")
                if not validation_safe:
                    reasons.append("a protected validation metric regressed")
                if not holdout_safe:
                    reasons.append("a protected holdout metric regressed")
                reason = "; ".join(reasons)

        version = self.store.save_skill_version(
            skill_name, prompt.strip(), candidate_metrics["score"], decision == "activated"
        )
        run = {
            "id": str(uuid.uuid4()),
            "skill_name": skill_name,
            "candidate_version": version["version"],
            "baseline_version": active["version"] if active else None,
            "decision": decision,
            "candidate_score": candidate_metrics["score"],
            "baseline_score": baseline_metrics["score"],
            "metrics": {
                "candidate": candidate_metrics,
                "baseline": baseline_metrics,
                "candidate_holdout": self._redact_holdout_metrics(candidate_holdout),
                "baseline_holdout": self._redact_holdout_metrics(baseline_holdout),
                "safety": safety,
                "gates": gates,
                "reason": reason,
                "reproducibility": {
                    "evaluation_schema_version": 2,
                    "candidate_prompt_sha256": self._sha256(prompt.strip()),
                    "baseline_prompt_sha256": self._sha256(baseline_prompt),
                    "validation_dataset_sha256": self._dataset_fingerprint(cases),
                    "holdout_dataset_sha256": self._dataset_fingerprint(holdout_cases),
                    "validation_case_ids": [case.get("id") for case in cases],
                    "holdout_case_count": len(holdout_cases),
                },
                "external_regression_score_ignored": regression_score is not None,
            },
            "created_at": utc_now(),
        }
        self.store.save_evolution_run(run)
        return {
            "version": version,
            "decision": decision,
            "reason": reason,
            "candidate": candidate_metrics,
            "baseline": baseline_metrics,
            "candidate_holdout": self._redact_holdout_metrics(candidate_holdout),
            "baseline_holdout": self._redact_holdout_metrics(baseline_holdout),
            "safety": safety,
            "gates": gates,
            "run_id": run["id"],
        }

    def rollback(self, skill_name: str, version: int) -> bool:
        with self._lock:
            return self.store.activate_skill_version(skill_name, version)

    def auto_propose(self, skill_name: str = "llm-review") -> Dict[str, Any]:
        cases = self.store.list_failure_cases(True, 100)
        active = self.store.get_active_skill_version(skill_name)
        base = active["prompt"] if active else DEFAULT_PROMPT
        counts = {}
        for case in cases:
            counts[case["category"]] = counts.get(case["category"], 0) + 1
        directives = []
        if counts.get("false_positive"):
            directives.append("Avoid style-only findings and require direct evidence from an added line.")
        if counts.get("missed_issue"):
            directives.append("Check boundary conditions, authorization, input validation and error paths explicitly.")
        if counts.get("bad_fix"):
            directives.append("Propose minimal fixes that preserve behavior and always include a regression test.")
        if counts.get("execution_error"):
            directives.append("Keep output valid JSON and follow the requested schema exactly.")
        additions = [directive for directive in directives if directive.lower() not in base.lower()]
        if not additions:
            return {
                "version": None,
                "decision": "deferred",
                "reason": "no new supported learning signal was found in unresolved feedback",
                "candidate": self._empty_metrics(0),
                "baseline": self._empty_metrics(0),
                "candidate_holdout": self._empty_metrics(0),
                "baseline_holdout": self._empty_metrics(0),
                "safety": self.safety_evaluate(base),
                "run_id": None,
                "failure_cases_used": len(cases),
                "learned_categories": counts,
            }
        candidate = base.rstrip() + (
            "\n\nLearned constraints:\n- " + "\n- ".join(additions)
        )
        result = self.propose(skill_name, candidate)
        result["failure_cases_used"] = len(cases)
        result["learned_categories"] = counts
        if result["decision"] == "activated":
            self.store.resolve_failure_cases([case["id"] for case in cases])
        return result

    @staticmethod
    def _empty_metrics(case_count: int) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "reviewer": "",
            "score": 0.0, "precision": 0.0, "recall": 0.0, "f1": 0.0,
            "severity_accuracy": 0.0, "high_severity_recall": 0.0, "clean_accuracy": 0.0,
            "cases": case_count, "positive_cases": 0, "clean_cases": 0,
            "expected_findings": 0, "predicted_findings": 0,
            "successful_cases": 0, "success_rate": 0.0, "errors": [], "case_results": [],
        }

    def _non_regressing(self, candidate: Dict[str, Any], baseline: Dict[str, Any]) -> bool:
        protected = ["score", "precision", "recall", "high_severity_recall"]
        if baseline.get("positive_cases", 0):
            protected.append("severity_accuracy")
        if baseline.get("clean_cases", 0):
            protected.append("clean_accuracy")
        return all(
            float(candidate.get(metric, 0.0)) + self.max_metric_regression
            >= float(baseline.get(metric, 0.0))
            for metric in protected
        )

    @staticmethod
    def _redact_holdout_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
        redacted = {
            key: value for key, value in metrics.items()
            if key not in {"errors", "case_results"}
        }
        redacted["error_count"] = len(metrics.get("errors", []))
        return redacted

    @staticmethod
    def _sha256(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _dataset_fingerprint(cls, cases: List[dict]) -> str:
        canonical = [
            {
                "name": case.get("name"),
                "split": case.get("split"),
                "diff": case.get("diff"),
                "expected": case.get("expected", []),
            }
            for case in cases
        ]
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls._sha256(payload)
