import json
import unittest
import urllib.error
from contextlib import contextmanager
from io import BytesIO

from evoagent import reviewer as reviewer_module
from evoagent.diff_parser import parse_unified_diff
from evoagent.models import Finding, Severity
from evoagent.reviewer import (
    CompositeReviewer,
    LocalRuleReviewer,
    OpenAICompatibleReviewer,
)


class LocalReviewerTests(unittest.TestCase):
    def test_detects_security_findings_only_on_added_lines(self):
        diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
-eval(old_input)
+password = "super-secret"
+eval(user_input)
 safe = True
"""
        findings = LocalRuleReviewer().review(diff, parse_unified_diff(diff))
        self.assertEqual({"SEC-EVAL", "SEC-HARDCODED-SECRET"}, {item.rule_id for item in findings})
        self.assertTrue(all(item.line in {1, 2} for item in findings))

    def test_detects_shell_sql_except_and_print_rules(self):
        diff = """--- a/svc.py
+++ b/svc.py
@@ -0,0 +1,4 @@
+run(cmd, shell=True)
+cursor.execute("select * from t where x=" + raw)
+print(secret)
+except Exception: pass
"""
        rules = {
            item.rule_id for item in LocalRuleReviewer().review(diff, parse_unified_diff(diff))
        }
        self.assertEqual(
            {"SEC-SUBPROCESS-SHELL", "SEC-SQL-CONCAT", "REL-DEBUG-PRINT", "REL-EMPTY-EXCEPT"},
            rules,
        )

    def test_lock_and_generated_files_are_skipped(self):
        # The .lock line is deliberately secret-shaped to prove the path skip
        # short-circuits before any rule runs. It is a synthetic fixture, so the
        # trailing marker keeps gitleaks from flagging it as a real credential.
        diff = """--- a/requirements.lock
+++ b/requirements.lock
@@ -0,0 +1,1 @@
+token = "abcdef123456"  # gitleaks:allow
"""
        self.assertEqual([], LocalRuleReviewer().review(diff, parse_unified_diff(diff)))

    def test_duplicate_hits_on_same_line_are_reported_once(self):
        diff = """--- a/a.py
+++ b/a.py
@@ -0,0 +1,1 @@
+x = eval(eval(y))
"""
        findings = LocalRuleReviewer().review(diff, parse_unified_diff(diff))
        self.assertEqual(1, len(findings))
        self.assertEqual("SEC-EVAL", findings[0].rule_id)


@contextmanager
def _fake_urlopen(payload: dict):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    yield _Resp()


_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,1 @@\n+eval(user_input)\n"


def _wrap(findings: list[dict]) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"findings": findings})}}]}


class OpenAIReviewerTests(unittest.TestCase):
    def setUp(self):
        self.reviewer = OpenAICompatibleReviewer("https://llm.example/v1", "key", "m")
        self.parsed = parse_unified_diff(_DIFF)

    def _run(self, payload, monkey):
        original = reviewer_module.urllib.request.urlopen
        reviewer_module.urllib.request.urlopen = monkey  # type: ignore[assignment]
        try:
            return self.reviewer.review(_DIFF, self.parsed)
        finally:
            reviewer_module.urllib.request.urlopen = original

    def test_parses_findings_on_valid_added_locations(self):
        payload = _wrap(
            [
                {
                    "rule_id": "LLM-1",
                    "severity": "high",
                    "title": "danger",
                    "path": "a.py",
                    "line": 1,
                    "evidence": "eval(user_input)",
                    "confidence": 0.9,
                }
            ]
        )
        findings = self._run(payload, lambda *a, **k: _fake_urlopen(payload))
        self.assertEqual(1, len(findings))
        self.assertEqual(Severity.HIGH, findings[0].severity)

    def test_findings_outside_added_lines_are_dropped(self):
        payload = _wrap([{"rule_id": "X", "severity": "high", "path": "a.py", "line": 999}])
        self.assertEqual([], self._run(payload, lambda *a, **k: _fake_urlopen(payload)))

    def test_unknown_severity_falls_back_to_medium(self):
        payload = _wrap([{"rule_id": "X", "severity": "spicy", "path": "a.py", "line": 1}])
        findings = self._run(payload, lambda *a, **k: _fake_urlopen(payload))
        self.assertEqual(Severity.MEDIUM, findings[0].severity)

    def test_confidence_is_clamped(self):
        payload = _wrap([{"severity": "low", "path": "a.py", "line": 1, "confidence": 5}])
        findings = self._run(payload, lambda *a, **k: _fake_urlopen(payload))
        self.assertEqual(1.0, findings[0].confidence)

    def test_http_error_is_wrapped(self):
        def _raise(*a, **k):
            raise urllib.error.HTTPError(
                "https://llm.example", 500, "boom", {}, BytesIO(b"server on fire")
            )

        with self.assertRaises(RuntimeError) as ctx:
            self._run(None, _raise)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_transport_error_is_wrapped(self):
        def _raise(*a, **k):
            raise urllib.error.URLError("no route")

        with self.assertRaises(RuntimeError):
            self._run(None, _raise)

    def test_non_json_model_content_is_wrapped(self):
        payload = {"choices": [{"message": {"content": "not json"}}]}
        with self.assertRaises(RuntimeError) as ctx:
            self._run(payload, lambda *a, **k: _fake_urlopen(payload))
        self.assertIn("invalid JSON", str(ctx.exception))


def _finding(rule_id, severity, line=1, path="a.py"):
    return Finding(rule_id, severity, "t", "e", path, line, "ev", "f", "test", 0.9)


class CompositeReviewerTests(unittest.TestCase):
    def test_merges_and_sorts_by_severity(self):
        class A:
            name = "a"

            def review(self, diff, parsed):
                return [_finding("LOW", Severity.LOW, line=2)]

        class B:
            name = "b"

            def review(self, diff, parsed):
                return [_finding("CRIT", Severity.CRITICAL, line=1)]

        merged = CompositeReviewer([A(), B()]).review("", parse_unified_diff(_DIFF))
        self.assertEqual(["CRIT", "LOW"], [item.rule_id for item in merged])
        self.assertEqual("a+b", CompositeReviewer([A(), B()]).name)

    def test_one_reviewer_failing_does_not_sink_the_others(self):
        class Ok:
            name = "ok"

            def review(self, diff, parsed):
                return [_finding("OK", Severity.HIGH)]

        class Bad:
            name = "bad"

            def review(self, diff, parsed):
                raise RuntimeError("down")

        merged = CompositeReviewer([Ok(), Bad()]).review("", parse_unified_diff(_DIFF))
        self.assertEqual(["OK"], [item.rule_id for item in merged])

    def test_raises_when_every_reviewer_fails(self):
        class Bad:
            name = "bad"

            def review(self, diff, parsed):
                raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            CompositeReviewer([Bad(), Bad()]).review("", parse_unified_diff(_DIFF))

    def test_same_location_and_rule_is_deduplicated(self):
        class A:
            name = "a"

            def review(self, diff, parsed):
                return [_finding("DUP", Severity.HIGH)]

        class B:
            name = "b"

            def review(self, diff, parsed):
                return [_finding("DUP", Severity.HIGH)]

        merged = CompositeReviewer([A(), B()]).review("", parse_unified_diff(_DIFF))
        self.assertEqual(1, len(merged))


if __name__ == "__main__":
    unittest.main()
