import os
import unittest
from types import SimpleNamespace

from evoagent.proof import EvidenceLevel, ProofRunner, _materialize, unified_patch

REPRO = "run-reproduction"
REGRESS = "run-regression"


def _read_all(root):
    text = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            with open(os.path.join(base, name), encoding="utf-8") as handle:
                text.append(handle.read())
    return "\n".join(text)


def _factory():
    """Fake verifier driven by marker strings in the materialised worktree, so
    tests can exercise every rung — including infra outcomes (timeout/error) —
    deterministically without executing anything.

    Reproduction command markers: BUG->failed, RTIMEOUT->timeout, RERROR->error.
    Regression command markers: REGRESS->failed, GERROR->error.
    """

    def factory(command):
        def verify_worktree(root):
            text = _read_all(root)
            if command == REGRESS:
                if "GERROR" in text:
                    status = "error"
                elif "REGRESS" in text:
                    status = "failed"
                else:
                    status = "passed"
            elif "RTIMEOUT" in text:
                status = "timeout"
            elif "RERROR" in text:
                status = "error"
            elif "BUG" in text:
                status = "failed"
            else:
                status = "passed"
            return {
                "passed": status == "passed",
                "status": status,
                "checks": [{"name": "cmd", "detail": "output", "status": status}],
                "duration_seconds": 0.01,
            }

        return SimpleNamespace(verify_worktree=verify_worktree)

    return factory


class UnifiedPatchTests(unittest.TestCase):
    def test_modified_file_produces_diff(self):
        patch = unified_patch({"a.py": "x = 1\n"}, {"a.py": "x = 2\n"})
        self.assertIn("-x = 1", patch)
        self.assertIn("+x = 2", patch)

    def test_identical_files_have_empty_patch(self):
        self.assertEqual("", unified_patch({"a.py": "x\n"}, {"a.py": "x\n"}))

    def test_added_and_removed_files(self):
        patch = unified_patch({"gone.py": "old\n"}, {"new.py": "new\n"})
        self.assertIn("a/gone.py", patch)
        self.assertIn("b/new.py", patch)

    def test_files_without_trailing_newline_do_not_merge_lines(self):
        patch = unified_patch({"a.py": "x = 1"}, {"a.py": "x = 2"})
        self.assertIn("-x = 1\n", patch)
        self.assertIn("+x = 2\n", patch)


class ProofLadderTests(unittest.TestCase):
    def setUp(self):
        self.runner = ProofRunner(_factory())

    def test_l1_without_reproduction(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "ok\n"})
        self.assertEqual(int(EvidenceLevel.L1_STATIC), result["evidence_level"])
        self.assertIn("static analysis only", result["note"])

    def test_l1_when_reproduction_does_not_fail_on_original(self):
        result = self.runner.prove({"a.py": "ok\n"}, {"a.py": "ok\n"}, REPRO)
        self.assertEqual(int(EvidenceLevel.L1_STATIC), result["evidence_level"])
        self.assertIn("unproven", result["note"])

    def test_l2_reproduced_but_patch_does_not_fix(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "still BUG\n"}, REPRO)
        self.assertEqual(int(EvidenceLevel.L2_REPRODUCED), result["evidence_level"])

    def test_l3_fix_verified_without_regression_command(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "fixed\n"}, REPRO)
        self.assertEqual(int(EvidenceLevel.L3_FIX_VERIFIED), result["evidence_level"])
        self.assertIn("L4 is not claimed", result["note"])

    def test_l3_capped_when_regression_fails(self):
        result = self.runner.prove(
            {"a.py": "BUG\n"}, {"a.py": "fixed but REGRESS\n"}, REPRO, REGRESS
        )
        self.assertEqual(int(EvidenceLevel.L3_FIX_VERIFIED), result["evidence_level"])
        self.assertIn("regression suite failed", result["note"])

    def test_l4_regression_clean(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "fixed\n"}, REPRO, REGRESS)
        self.assertEqual(int(EvidenceLevel.L4_REGRESSION_CLEAN), result["evidence_level"])
        self.assertEqual("L4-regression-clean", result["evidence_label"])

    def test_patch_is_included_in_every_result(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "fixed\n"}, REPRO, REGRESS)
        self.assertIn("+fixed", result["patch"])
        self.assertEqual(3, len(result["steps"]))

    def test_infra_failure_on_original_is_inconclusive_l1(self):
        # A timeout on the original code must NOT be sold as a reproduced bug.
        result = self.runner.prove({"a.py": "RTIMEOUT\n"}, {"a.py": "fixed\n"}, REPRO)
        self.assertEqual(int(EvidenceLevel.L1_STATIC), result["evidence_level"])
        self.assertIn("inconclusive", result["note"])

    def test_infra_failure_verifying_patch_caps_at_l2(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "RERROR\n"}, REPRO)
        self.assertEqual(int(EvidenceLevel.L2_REPRODUCED), result["evidence_level"])
        self.assertIn("could not be verified", result["note"])

    def test_infra_failure_in_regression_caps_at_l3(self):
        result = self.runner.prove({"a.py": "BUG\n"}, {"a.py": "GERROR\n"}, REPRO, REGRESS)
        self.assertEqual(int(EvidenceLevel.L3_FIX_VERIFIED), result["evidence_level"])
        self.assertIn("could not be run", result["note"])


class MaterializeSafetyTests(unittest.TestCase):
    def test_rejects_path_traversal(self):
        import tempfile

        root = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            _materialize({"../escape.py": "x"}, root)

    def test_rejects_non_string_content(self):
        import tempfile

        root = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            _materialize({"a.py": 123}, root)


if __name__ == "__main__":
    unittest.main()
