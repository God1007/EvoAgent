import unittest

from evoagent.models import FINDING_TEXT_LIMITS, Finding, ReviewReport, Severity


def _finding(**overrides):
    base = dict(
        rule_id="SEC-EVAL",
        severity=Severity.HIGH,
        title="t",
        explanation="e",
        path="app/service.py",
        line=42,
        evidence="eval(user_input)",
        fix="f",
        test="t",
    )
    base.update(overrides)
    return Finding(**base)


class FindingFingerprintTests(unittest.TestCase):
    def test_stable_across_line_number_shift(self):
        self.assertEqual(_finding(line=42).fingerprint(), _finding(line=999).fingerprint())

    def test_stable_across_reindentation(self):
        # The same statement wrapped in a new block gains leading indentation and
        # trailing newline whitespace, but is the same logical finding.
        a = _finding(evidence="eval(user_input)")
        b = _finding(evidence="\n        eval(user_input)\n")
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_changes_with_rule(self):
        self.assertNotEqual(
            _finding(rule_id="SEC-EVAL").fingerprint(),
            _finding(rule_id="SEC-SUBPROCESS-SHELL").fingerprint(),
        )

    def test_changes_with_path(self):
        self.assertNotEqual(
            _finding(path="a.py").fingerprint(), _finding(path="b.py").fingerprint()
        )

    def test_changes_with_evidence_content(self):
        self.assertNotEqual(
            _finding(evidence="eval(a)").fingerprint(),
            _finding(evidence="eval(b)").fingerprint(),
        )

    def test_changes_with_title(self):
        self.assertNotEqual(
            _finding(title="A", evidence="").fingerprint(),
            _finding(title="B", evidence="").fingerprint(),
        )

    def test_field_values_cannot_be_confused_with_a_delimiter(self):
        self.assertNotEqual(
            _finding(path="a|b", rule_id="c", evidence="x").fingerprint(),
            _finding(path="a", rule_id="b|c", evidence="x").fingerprint(),
        )

    def test_identical_findings_share_fingerprint_by_design(self):
        self.assertEqual(
            _finding(line=1).fingerprint(),
            _finding(line=2).fingerprint(),
        )

    def test_to_dict_exposes_fingerprint(self):
        finding = _finding()
        self.assertEqual(finding.fingerprint(), finding.to_dict()["fingerprint"])


class ScopedFingerprintTests(unittest.TestCase):
    def test_same_finding_differs_across_repositories(self):
        finding = _finding()
        self.assertNotEqual(
            finding.scoped_fingerprint("org/a"),
            finding.scoped_fingerprint("org/b"),
        )

    def test_same_repo_and_finding_is_stable_across_lines(self):
        self.assertEqual(
            _finding(line=1).scoped_fingerprint("org/a"),
            _finding(line=900).scoped_fingerprint("org/a"),
        )

    def test_symbol_override_participates_in_identity(self):
        finding = _finding()
        self.assertNotEqual(
            finding.scoped_fingerprint("org/a", symbol="mod.f"),
            finding.scoped_fingerprint("org/a", symbol="mod.g"),
        )

    def test_symbol_defaults_to_path(self):
        finding = _finding(path="app/x.py")
        self.assertEqual(
            finding.scoped_fingerprint("org/a"),
            finding.scoped_fingerprint("org/a", symbol="app/x.py"),
        )

    def test_scoped_differs_from_unscoped(self):
        finding = _finding()
        self.assertNotEqual(finding.fingerprint(), finding.scoped_fingerprint("org/a"))


class FindingFromDictTests(unittest.TestCase):
    def test_round_trip_preserves_fields_and_drops_derived_key(self):
        original = _finding()
        restored = Finding.from_dict(original.to_dict())
        self.assertEqual(original, restored)
        # Severity is a (str, Enum), so string==member; assert the real type too
        # or a regression to a bare string would slip past equality checks.
        self.assertIsInstance(restored.severity, Severity)

    def test_accepts_uppercase_severity_string(self):
        finding = Finding.from_dict({**_finding().to_dict(), "severity": "HIGH"})
        self.assertEqual(Severity.HIGH, finding.severity)

    def test_unknown_severity_falls_back_to_medium(self):
        finding = Finding.from_dict({**_finding().to_dict(), "severity": "spicy"})
        self.assertEqual(Severity.MEDIUM, finding.severity)

    def test_missing_severity_defaults_to_medium(self):
        payload = _finding().to_dict()
        del payload["severity"]
        self.assertEqual(Severity.MEDIUM, Finding.from_dict(payload).severity)

    def test_missing_required_field_raises_clear_error(self):
        payload = _finding().to_dict()
        del payload["rule_id"]
        with self.assertRaises(ValueError) as ctx:
            Finding.from_dict(payload)
        self.assertIn("rule_id", str(ctx.exception))

    def test_rejects_types_that_would_break_downstream_review_nodes(self):
        for field, value in (("path", []), ("line", "1"), ("confidence", float("nan"))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                Finding.from_dict({**_finding().to_dict(), field: value})

    def test_rejects_text_that_cannot_be_persisted_as_postgres_json(self):
        for field, value in (("title", "bad\x00title"), ("explanation", "bad\ud800text")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "valid UTF-8"):
                Finding.from_dict({**_finding().to_dict(), field: value})

    def test_rejects_rule_ids_that_cannot_be_stable_identifiers(self):
        for rule_id in ("", " SEC-EVAL", "SEC-EVAL ", "SEC\tEVAL", "SEC\x00EVAL", "X" * 81):
            with self.subTest(rule_id=rule_id), self.assertRaises(ValueError):
                Finding.from_dict({**_finding().to_dict(), "rule_id": rule_id})

    def test_rejects_oversized_reviewer_text_at_the_shared_boundary(self):
        for field, limit in FINDING_TEXT_LIMITS.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                Finding.from_dict({**_finding().to_dict(), field: "X" * (limit + 1)})


class ReviewReportFromDictTests(unittest.TestCase):
    def test_round_trip_preserves_valid_report(self):
        report = ReviewReport("org/repo", 7, "done", "high", [_finding()], ["a.py"], "rules")

        self.assertEqual(report, ReviewReport.from_dict(report.to_dict()))

    def test_rejects_ambiguous_checkpoint_types(self):
        valid = ReviewReport("org/repo", 7, "done", "low").to_dict()
        for field, value, message in (
            ("repository", 7, "text fields"),
            ("summary", [], "text fields"),
            ("risk", "urgent", "repository and risk"),
            ("pull_request", True, "positive integer"),
            ("findings", {}, "list of objects"),
            ("findings", ["finding"], "list of objects"),
            ("files_reviewed", "a.py", "list of strings"),
            ("files_reviewed", [7], "list of strings"),
            ("reviewer", 7, "text fields"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    ReviewReport.from_dict({**valid, field: value})


if __name__ == "__main__":
    unittest.main()
