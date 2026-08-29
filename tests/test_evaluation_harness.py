import hashlib
import json
import os
import tempfile
import unittest

from evoagent.evaluation_benchmark import (
    baseline_reviewer,
    candidate_reviewer,
    generate_controlled_pr_cases,
)
from evoagent.evaluation_dataset import dataset_fingerprint, load_jsonl
from evoagent.evaluation_harness import (
    EndToEndEvaluationHarness,
    FixtureRepairer,
    comparison_summary,
    one_to_one_match,
)
from evoagent.evaluation_provenance import audit_dataset_provenance
from evoagent.models import Finding, Severity
from scripts.run_e2e_evaluation import reproducibility_metadata


class EndToEndEvaluationTests(unittest.TestCase):
    @staticmethod
    def independently_labelled_public_cases():
        cases = generate_controlled_pr_cases()
        packet_hashes = []
        for case in cases:
            case["schema_version"] = 2
            case_id = case["id"]
            hashes = [
                hashlib.sha256((case_id + role).encode()).hexdigest()
                for role in ("annotator-a", "annotator-b", "adjudicator-c")
            ]
            packet_hashes.extend(hashes)
            case["source"] = {
                "kind": "public-github-pr",
                "public_url": "https://github.com/%s/pull/%d"
                % (case["repository"], case["pull_request"]),
                "head_sha": hashlib.sha256(case_id.encode()).hexdigest()[:40],
                "diff_sha256": hashlib.sha256(case["diff"].encode()).hexdigest(),
                "retrieved_at": "2026-08-17T00:00:00Z",
                "rights": {
                    "usage_basis": "repository-license",
                    "spdx_id": "Apache-2.0",
                    "review_status": "approved",
                    "data_review_status": "approved",
                    "review_reference": "LEGAL-EVAL-1",
                },
            }
            case["annotation"] = {
                "schema_version": 1,
                "protocol_id": "blind-v1",
                "blind_to_system_output": True,
                "independent_annotators": ["annotator-a", "annotator-b"],
                "adjudicator_id": "adjudicator-c",
                "packet_sha256s": sorted(hashes),
                "pair_agreement": [
                    {
                        "left_reviewer_id": "annotator-a",
                        "right_reviewer_id": "annotator-b",
                        "left_findings": len(case["expected_findings"]),
                        "right_findings": len(case["expected_findings"]),
                        "matched_findings": len(case["expected_findings"]),
                        "finding_f1": 1.0,
                        "severity_matches": len(case["expected_findings"]),
                        "severity_agreement": 1.0 if case["expected_findings"] else 0.0,
                    }
                ],
            }
        bundle = hashlib.sha256(
            json.dumps(
                sorted(packet_hashes),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        evidence = {
            "schema_version": 1,
            "status": "compiled",
            "case_count": len(cases),
            "repository_count": len({case["repository"] for case in cases}),
            "protocol_ids": ["blind-v1"],
            "annotation_bundle_sha256": bundle,
            "dataset_sha256": dataset_fingerprint(cases),
            "agreement": {
                "annotator_pair_count": len(cases),
                "macro_finding_f1": 1.0,
                "positive_pair_count": sum(bool(case["expected_findings"]) for case in cases),
                "positive_pair_macro_finding_f1": 1.0,
                "both_clean_pair_count": sum(not case["expected_findings"] for case in cases),
                "matched_severity_agreement": 1.0,
            },
        }
        return cases, evidence

    def test_generated_dataset_has_repository_level_split_and_expected_counts(self):
        cases = generate_controlled_pr_cases()
        self.assertEqual(100, len(cases))
        self.assertEqual(40, sum(bool(item["expected_findings"]) for item in cases))
        self.assertEqual(60, sum(not item["expected_findings"] for item in cases))
        validation_repos = {item["repository"] for item in cases if item["split"] == "validation"}
        holdout_repos = {item["repository"] for item in cases if item["split"] == "holdout"}
        self.assertEqual(8, len(validation_repos))
        self.assertEqual(2, len(holdout_repos))
        self.assertFalse(validation_repos & holdout_repos)
        self.assertEqual(
            {"synthetic-controlled"},
            {item["source"]["kind"] for item in cases},
        )

    def test_report_reproducibility_binds_source_runtime_and_lockfile(self):
        metadata = reproducibility_metadata()

        self.assertEqual(64, len(metadata["application_source_sha256"]))
        self.assertEqual(64, len(metadata["requirements_lock_sha256"]))
        self.assertRegex(metadata["python_version"], r"^\d+\.\d+\.\d+$")

    def test_harness_rejects_truthy_non_boolean_repair_label(self):
        case = next(item for item in generate_controlled_pr_cases() if item["expected_findings"])
        case["repair_validation"]["auto_fixable"] = "false"

        with self.assertRaisesRegex(ValueError, "auto_fixable must be a boolean"):
            EndToEndEvaluationHarness(repairer=FixtureRepairer()).run(candidate_reviewer(), [case])

    def test_one_to_one_matching_counts_duplicate_prediction_once(self):
        expected = [
            {
                "path": "src/a.py",
                "start_line": 10,
                "end_line": 12,
                "cwe": "CWE-95",
                "severity": "critical",
            }
        ]
        predicted = [
            Finding(
                "SEC-EVAL",
                Severity.CRITICAL,
                "a",
                "long enough explanation",
                "src/a.py",
                line,
                "eval(x)",
                "replace eval safely",
                "add malicious input test",
                0.9,
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
        self.assertEqual(
            (27, 5, 13),
            (
                baseline["metrics"]["tp"],
                baseline["metrics"]["fp"],
                baseline["metrics"]["fn"],
            ),
        )
        self.assertEqual(
            (33, 7, 7),
            (
                candidate["metrics"]["tp"],
                candidate["metrics"]["fp"],
                candidate["metrics"]["fn"],
            ),
        )
        self.assertEqual(0.75, baseline["metrics"]["f1"])
        self.assertEqual(0.825, candidate["metrics"]["f1"])
        self.assertEqual(0.9474, candidate["metrics"]["high_risk_recall"])
        self.assertEqual(0.9167, candidate["metrics"]["clean_accuracy"])
        self.assertEqual(1.0, candidate["metrics"]["execution_success_rate"])
        self.assertEqual(24, candidate["metrics"]["repair_eligible"])
        self.assertEqual(24, candidate["metrics"]["repair_attempted"])
        self.assertEqual(24, candidate["metrics"]["repair_passed"])
        self.assertEqual(9, candidate["metrics"]["repair_abstained"])
        self.assertEqual(1.0, candidate["metrics"]["safe_fix_rate"])
        self.assertEqual(0.6, candidate["metrics"]["e2e_security_fix_rate"])
        self.assertEqual(100, candidate["by_language"]["Python"]["cases"])
        self.assertEqual(0.825, candidate["by_language"]["Python"]["f1"])
        self.assertEqual(40, candidate["confidence_calibration"]["predictions"])
        self.assertEqual(0, candidate["confidence_calibration"]["invalid_confidences"])
        self.assertIsNotNone(candidate["confidence_calibration"]["brier_score"])
        self.assertIn("CWE-95", candidate["by_cwe"])
        self.assertIn("SEC-EVAL", candidate["by_rule"])
        gate = comparison_summary(baseline, candidate)["release_gate"]
        self.assertTrue(gate["quantitative_passed"])
        self.assertTrue(gate["gates"]["safe_fix_rate"]["passed"])
        self.assertTrue(gate["gates"]["e2e_security_fix_rate"]["passed"])
        self.assertFalse(gate["production_activation_allowed"])
        self.assertFalse(gate["passed"])

    def test_invalid_finding_confidence_is_reported_not_silently_clamped(self):
        case = next(case for case in generate_controlled_pr_cases() if case["expected_findings"])

        class ReviewerWithInvalidConfidence:
            name = "invalid-confidence"

            def review(self, _diff, _parsed):
                truth = case["expected_findings"][0]
                return [
                    Finding(
                        "SEC-EVAL",
                        Severity.CRITICAL,
                        "finding",
                        "long enough explanation",
                        truth["path"],
                        truth["start_line"],
                        "eval(value)",
                        "replace eval safely",
                        "add a regression test",
                        1.5,
                    )
                ]

        report = EndToEndEvaluationHarness().run(ReviewerWithInvalidConfidence(), [case])

        self.assertEqual(1, report["confidence_calibration"]["invalid_confidences"])
        self.assertEqual(0, report["confidence_calibration"]["valid_confidences"])

    def test_comparison_rejects_different_baseline_and_candidate_datasets(self):
        cases = generate_controlled_pr_cases()
        baseline = EndToEndEvaluationHarness().run(baseline_reviewer(), cases)
        candidate = EndToEndEvaluationHarness().run(candidate_reviewer(), cases)
        candidate = {**candidate, "dataset": {**candidate["dataset"], "sha256": "0" * 64}}

        gate = comparison_summary(baseline, candidate)["release_gate"]

        self.assertFalse(gate["gates"]["same_dataset"]["passed"])
        self.assertFalse(gate["quantitative_passed"])

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

    def test_production_provenance_requires_bound_independent_evidence(self):
        cases, evidence = self.independently_labelled_public_cases()

        audit = audit_dataset_provenance(cases, evidence)

        self.assertTrue(audit["production_ready"])
        self.assertTrue(all(check["passed"] for check in audit["checks"].values()))
        without_sidecar = audit_dataset_provenance(cases)
        self.assertFalse(without_sidecar["production_ready"])
        self.assertFalse(without_sidecar["checks"]["annotation_evidence_binding"]["passed"])

    def test_provenance_fails_closed_on_content_rights_or_blinding_tamper(self):
        cases, evidence = self.independently_labelled_public_cases()
        cases[0]["diff"] += "\n+tampered = True\n"
        cases[1]["source"]["rights"]["review_status"] = "pending"
        cases[2]["annotation"]["blind_to_system_output"] = False

        audit = audit_dataset_provenance(cases, evidence)

        self.assertFalse(audit["production_ready"])
        self.assertFalse(audit["checks"]["immutable_public_sources"]["passed"])
        self.assertFalse(audit["checks"]["approved_usage_and_data_review"]["passed"])
        self.assertFalse(audit["checks"]["independent_blind_annotations"]["passed"])
        self.assertFalse(audit["checks"]["annotation_evidence_binding"]["passed"])


if __name__ == "__main__":
    unittest.main()
