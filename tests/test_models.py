import unittest

from evoagent.models import Finding, Severity


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


if __name__ == "__main__":
    unittest.main()
