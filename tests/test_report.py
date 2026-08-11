import unittest

from evoagent.report import to_markdown


def _finding(**overrides):
    base = {
        "severity": "high",
        "title": "t",
        "path": "a.py",
        "line": 1,
        "rule_id": "R",
        "evidence": "x",
        "explanation": "e",
        "fix": "f",
        "test": "t",
    }
    base.update(overrides)
    return base


class ReportMarkdownTests(unittest.TestCase):
    def test_clean_report_has_no_findings_banner(self):
        md = to_markdown({"repository": "org/repo", "risk": "low", "findings": []})
        self.assertIn("✅ No actionable issue detected", md)
        self.assertIn("`org/repo`", md)

    def test_evidence_with_backticks_cannot_escape_the_code_fence(self):
        evidence = "legit()\n```\n## Injected heading\n- injected list"
        md = to_markdown(
            {"repository": "org/repo", "risk": "high", "findings": [_finding(evidence=evidence)]}
        )
        self.assertIn("````", md)
        block = md.split("````")
        self.assertEqual(3, len(block))
        self.assertIn("## Injected heading", block[1])

    def test_inline_fields_neutralise_backticks_and_newlines(self):
        md = to_markdown(
            {
                "repository": "org/repo",
                "risk": "high",
                "findings": [_finding(title="Evil\n# Pwned", path="a`b`.py", rule_id="R`x`")],
            }
        )
        self.assertNotIn("\n# Pwned", md)
        self.assertIn("### 1. 🔴 Evil \\# Pwned", md)
        self.assertIn("`a'b'.py:1`", md)
        self.assertIn("`R'x'`", md)

    def test_each_severity_renders_its_icon(self):
        for severity, icon in (
            ("critical", "🚨"),
            ("high", "🔴"),
            ("medium", "🟠"),
            ("low", "🟡"),
        ):
            md = to_markdown(
                {"repository": "o/r", "risk": severity, "findings": [_finding(severity=severity)]}
            )
            self.assertIn("### 1. %s" % icon, md)
            self.assertIn(severity.upper(), md)

    def test_unknown_severity_uses_bullet_fallback(self):
        md = to_markdown(
            {"repository": "o/r", "risk": "x", "findings": [_finding(severity="bogus")]}
        )
        self.assertIn("### 1. • ", md)

    def test_pull_request_number_appears_in_title(self):
        md = to_markdown({"repository": "o/r", "risk": "low", "pull_request": 42, "findings": []})
        self.assertIn("# EvoAgent PR Review — #42", md)

    def test_pull_request_number_is_escaped_against_injection(self):
        md = to_markdown(
            {"repository": "o/r", "risk": "low", "pull_request": "1 # x", "findings": []}
        )
        self.assertIn("1 \\# x", md)

    def test_free_text_fields_cannot_inject_block_markdown(self):
        md = to_markdown(
            {
                "repository": "org/repo",
                "risk": "high",
                "summary": "![owned](http://attacker/x.png)",
                "findings": [
                    _finding(
                        explanation="## pwned\n- item\n> quote\n|a|b|\n[x](http://e)",
                        fix="\n### heading injection",
                        test="\n- list injection",
                    )
                ],
            }
        )
        for injected in (
            "\n## pwned",
            "\n### heading injection",
            "\n- item",
            "\n- list injection",
            "\n> quote",
            "\n|a|b|",
        ):
            self.assertNotIn(injected, md)
        self.assertNotIn("![owned](http://attacker/x.png)", md)
        self.assertNotIn("[x](http://e)", md)
        self.assertIn("\\#\\# pwned", md)


if __name__ == "__main__":
    unittest.main()
