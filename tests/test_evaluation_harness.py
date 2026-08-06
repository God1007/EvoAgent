import os
import tempfile
import unittest

from evoagent.evaluation_benchmark import (
    baseline_reviewer,
    candidate_reviewer,
    generate_controlled_pr_cases,
)
from evoagent.evaluation_harness import (
    EndToEndEvaluationHarness,
    FixtureRepairer,
    comparison_summary,
    dataset_fingerprint,
    load_jsonl,
    one_to_one_match,
)
from evoagent.models import Finding, Severity


class EndToEndEvaluationTests(unittest.TestCase):
    def test_generated_dataset_has_repository_level_split_and_expected_counts(self):
        cases = generate_controlled_pr_cases()
        self.assertEqual(100, len(cases))
        self.assertEqual(40, sum(bool(item["expected_findings"]) for item in cases))
        self.assertEqual(60, sum(not item["expected_findings"] for item in cases))
        validation_repos = {
            item["repository"] for item in cases if item["split"] == "validation"
        }
        holdout_repos = {
            item["repository"] for item in cases if item["split"] == "holdout"
        }
        self.assertEqual(8, len(validation_repos))
        self.assertEqual(2, len(holdout_repos))
        self.assertFalse(validation_repos & holdout_repos)
        self.assertEqual(
            {"synthetic-controlled"},
            {item["source"]["kind"] for item in cases},
        )

    def test_one_to_one_matching_counts_duplicate_prediction_once(self):
        expected = [{
            "path": "src/a.py", "start_line": 10, "end_line": 12,
            "cwe": "CWE-95", "severity": "critical",
        }]
        predicted = [
            Finding(
                "SEC-EVAL", Severity.CRITICAL, "a", "long enough explanation",
                "src/a.py", line, "eval(x)", "replace eval safely",
                "add malicious input test", 0.9,
            )
            for line in (10, 11)
        ]
        matches = one_to_one_match(expected, predicted)
        self.assertEqual(1, len(matches))

    def test_benchmark_reproduces_target_metric_shape(self):
        cases = generate_controlled_pr_cases()
        baseline = EndToEndEvaluationHarness().run(baseline_reviewer(), cases)
        candidate = EndToEndEvaluationHarness(repairer=FixtureRepairer()).run(
            candidate_reviewer(), cases
        )
        self.assertEqual((25, 5, 15), (
            baseline["metrics"]["tp"],
            baseline["metrics"]["fp"],
            baseline["metrics"]["fn"],
        ))
        self.assertEqual((33, 7, 7), (
            candidate["metrics"]["tp"],
            candidate["metrics"]["fp"],
            candidate["metrics"]["fn"],
        ))
        self.assertEqual(0.7143, baseline["metrics"]["f1"])
        self.assertEqual(0.825, candidate["metrics"]["f1"])
        self.assertEqual(0.9474, candidate["metrics"]["high_risk_recall"])
        self.assertEqual(0.9167, candidate["metrics"]["clean_accuracy"])
        self.assertEqual(1.0, candidate["metrics"]["execution_success_rate"])
        self.assertEqual(0.7879, candidate["metrics"]["safe_fix_rate"])
        self.assertEqual(0.65, candidate["metrics"]["e2e_security_fix_rate"])
        gate = comparison_summary(baseline, candidate)["release_gate"]
        self.assertTrue(gate["quantitative_passed"])
        self.assertFalse(gate["production_activation_allowed"])
        self.assertFalse(gate["passed"])

    def test_dataset_round_trip_has_stable_fingerprint(self):
        cases = generate_controlled_pr_cases()
        handle, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(handle)
        try:
            import json
            with open(path, "w", encoding="utf-8") as output:
                for case in cases:
                    output.write(json.dumps(case, ensure_ascii=False) + "\n")
            loaded = load_jsonl(path)
            self.assertEqual(dataset_fingerprint(cases), dataset_fingerprint(loaded))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
