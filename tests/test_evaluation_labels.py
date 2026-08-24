import hashlib
import json
import os
import tempfile
import unittest

from evoagent.evaluation_benchmark import generate_controlled_pr_cases
from evoagent.evaluation_dataset import dataset_fingerprint, load_jsonl
from evoagent.evaluation_labels import compile_independent_annotations, load_records, main
from evoagent.evaluation_provenance import audit_dataset_provenance


def label_fields(findings):
    fields = ("path", "start_line", "end_line", "cwe", "severity")
    return [{field: finding[field] for field in fields} for finding in findings]


class IndependentAnnotationTests(unittest.TestCase):
    def setUp(self):
        generated = generate_controlled_pr_cases()
        selected = [
            next(case for case in generated if case["expected_findings"]),
            next(case for case in generated if not case["expected_findings"]),
        ]
        self.truth = {case["id"]: label_fields(case["expected_findings"]) for case in selected}
        self.cases = [
            {key: value for key, value in case.items() if key != "expected_findings"}
            for case in selected
        ]

    def packet(self, case_id, role, reviewer_id, findings=None, blind=True, protocol="blind-v1"):
        return {
            "schema_version": 1,
            "case_id": case_id,
            "role": role,
            "reviewer_id": reviewer_id,
            "protocol_id": protocol,
            "blind_to_system_output": blind,
            "findings": self.truth[case_id] if findings is None else findings,
        }

    def packets(self, disagree=False):
        packets = []
        for case in self.cases:
            case_id = case["id"]
            second = [] if disagree and self.truth[case_id] else self.truth[case_id]
            packets.extend(
                [
                    self.packet(case_id, "annotation", "annotator-a"),
                    self.packet(case_id, "annotation", "annotator-b", second),
                    self.packet(case_id, "adjudication", "adjudicator-c"),
                ]
            )
        return packets

    def test_compiles_blind_annotations_and_reproducible_evidence(self):
        compiled, evidence = compile_independent_annotations(self.cases, self.packets())

        self.assertEqual(2, len(compiled))
        self.assertEqual(dataset_fingerprint(compiled), evidence["dataset_sha256"])
        self.assertEqual(1.0, evidence["agreement"]["macro_finding_f1"])
        self.assertEqual(2, evidence["agreement"]["annotator_pair_count"])
        for case in compiled:
            self.assertEqual(2, case["schema_version"])
            self.assertEqual(self.truth[case["id"]], case["expected_findings"])
            self.assertEqual(3, len(case["annotation"]["packet_sha256s"]))
            self.assertEqual(
                ["annotator-a", "annotator-b"],
                case["annotation"]["independent_annotators"],
            )

        repeated, repeated_evidence = compile_independent_annotations(
            list(reversed(self.cases)), list(reversed(self.packets()))
        )
        self.assertEqual(compiled, repeated)
        self.assertEqual(evidence, repeated_evidence)

    def test_evaluation_jsonl_rejects_duplicate_evidence_fields(self):
        case = generate_controlled_pr_cases()[0]
        case_json = json.dumps(case)
        split = '"split": "%s"' % case["split"]
        packet_json = json.dumps(self.packets()[0])
        blind = '"blind_to_system_output": true'
        for loader, raw in (
            (load_jsonl, case_json.replace(split, '"split": "invalid", ' + split)),
            (
                load_records,
                packet_json.replace(blind, '"blind_to_system_output": false, ' + blind),
            ),
        ):
            with tempfile.NamedTemporaryFile("w", delete=False) as handle:
                handle.write(raw + "\n")
                path = handle.name
            self.addCleanup(os.unlink, path)
            with (
                self.subTest(loader=loader.__name__),
                self.assertRaisesRegex(ValueError, "invalid JSON"),
            ):
                loader(path)

    def test_records_disagreement_without_changing_adjudicated_truth(self):
        compiled, evidence = compile_independent_annotations(
            self.cases, self.packets(disagree=True)
        )

        self.assertEqual(0.5, evidence["agreement"]["macro_finding_f1"])
        self.assertEqual(0.0, evidence["agreement"]["positive_pair_macro_finding_f1"])
        self.assertEqual(1, evidence["agreement"]["both_clean_pair_count"])
        self.assertEqual(0.0, evidence["agreement"]["matched_severity_agreement"])
        risk = next(case for case in compiled if case["expected_findings"])
        self.assertEqual(self.truth[risk["id"]], risk["expected_findings"])

    def test_rejects_non_blind_or_non_independent_review(self):
        packets = self.packets()
        packets[0]["blind_to_system_output"] = False
        with self.assertRaisesRegex(ValueError, "blind to system output"):
            compile_independent_annotations(self.cases, packets)

        packets = self.packets()
        packets[2]["reviewer_id"] = "annotator-a"
        with self.assertRaisesRegex(ValueError, "adjudicator must be independent"):
            compile_independent_annotations(self.cases, packets)

    def test_rejects_answer_leakage_and_repository_split_leakage(self):
        leaked_answer = [{**self.cases[0], "expected_findings": []}, self.cases[1]]
        with self.assertRaisesRegex(ValueError, "already contains answer data"):
            compile_independent_annotations(leaked_answer, self.packets())

        leaked_split = [dict(case) for case in self.cases]
        leaked_split[1]["repository"] = leaked_split[0]["repository"]
        leaked_split[1]["split"] = (
            "holdout" if leaked_split[0]["split"] == "validation" else "validation"
        )
        with self.assertRaisesRegex(ValueError, "both validation and holdout"):
            compile_independent_annotations(leaked_split, self.packets())

    def test_cli_writes_loadable_dataset_and_matching_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            cases_path = os.path.join(directory, "cases.jsonl")
            annotations_path = os.path.join(directory, "annotations.jsonl")
            output_path = os.path.join(directory, "dataset.jsonl")
            evidence_path = os.path.join(directory, "evidence.json")
            for path, records in (
                (cases_path, self.cases),
                (annotations_path, self.packets()),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record) + "\n")

            status = main(
                [
                    "--cases",
                    cases_path,
                    "--annotations",
                    annotations_path,
                    "--output",
                    output_path,
                    "--evidence",
                    evidence_path,
                ]
            )

            self.assertEqual(0, status)
            compiled = load_jsonl(output_path)
            with open(evidence_path, encoding="utf-8") as handle:
                evidence = json.load(handle)
            self.assertEqual(dataset_fingerprint(compiled), evidence["dataset_sha256"])

    def test_compiler_sidecar_satisfies_the_structural_production_audit(self):
        raw_cases = []
        packets = []
        for case in generate_controlled_pr_cases():
            truth = label_fields(case["expected_findings"])
            raw = {key: value for key, value in case.items() if key != "expected_findings"}
            raw["source"] = {
                "kind": "public-github-pr",
                "public_url": "https://github.com/%s/pull/%d"
                % (case["repository"], case["pull_request"]),
                "head_sha": hashlib.sha256(case["id"].encode()).hexdigest()[:40],
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
            raw_cases.append(raw)
            for role, reviewer in (
                ("annotation", "annotator-a"),
                ("annotation", "annotator-b"),
                ("adjudication", "adjudicator-c"),
            ):
                packets.append(
                    {
                        "schema_version": 1,
                        "case_id": case["id"],
                        "role": role,
                        "reviewer_id": reviewer,
                        "protocol_id": "blind-v1",
                        "blind_to_system_output": True,
                        "findings": truth,
                    }
                )

        compiled, evidence = compile_independent_annotations(raw_cases, packets)
        audit = audit_dataset_provenance(compiled, evidence)

        self.assertTrue(audit["production_ready"])


if __name__ == "__main__":
    unittest.main()
