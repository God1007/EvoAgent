import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from evoagent.agents import (
    MAX_REVIEW_AGENT_NAME_CHARS,
    MAX_REVIEW_AGENTS,
    FilteredAgent,
    MultiAgentCoordinator,
)
from evoagent.diff_parser import parse_unified_diff
from evoagent.metrics import Metrics
from evoagent.model_gateway import ModelGovernanceContext
from evoagent.models import Finding, Severity
from evoagent.reviewer import (
    MAX_REVIEWER_FINDINGS,
    GatewayReviewer,
    LocalRuleReviewer,
    Reviewer,
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

    def test_detects_yaml_load_and_explicit_insecure_cookie(self):
        diff = """--- a/web.py
+++ b/web.py
@@ -0,0 +1,2 @@
+payload = yaml.load(raw)
+response.set_cookie("sid", value, secure=False)
"""
        rules = {
            item.rule_id for item in LocalRuleReviewer().review(diff, parse_unified_diff(diff))
        }
        self.assertEqual({"SEC-YAML-LOAD", "SEC-INSECURE-COOKIE"}, rules)

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


_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1,1 @@\n+eval(user_input)\n"


class _Gateway:
    configured = True

    def __init__(self):
        self.content = ""
        self.request = None

    @staticmethod
    def route_info():
        return {"provider": "provider-a", "model": "model-a"}

    def complete(self, request):
        self.request = request
        return SimpleNamespace(content=self.content)


class GatewayReviewerTests(unittest.TestCase):
    def setUp(self):
        self.gateway = _Gateway()
        self.reviewer = GatewayReviewer(
            self.gateway,
            lambda _task_id: ModelGovernanceContext(
                "tenant-a", "org/repo", ("provider-a",), ("model-a",), "eu"
            ),
        )
        self.parsed = parse_unified_diff(_DIFF)

    def _run(self, findings):
        self.gateway.content = json.dumps({"findings": findings})
        return self.reviewer.review_with_context("task-1", _DIFF, self.parsed)

    def test_parses_findings_on_valid_added_locations(self):
        findings = self._run(
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
        self.assertEqual(1, len(findings))
        self.assertEqual(Severity.HIGH, findings[0].severity)
        self.assertEqual(
            ("tenant-a", "org/repo", "review"),
            (
                self.gateway.request.tenant_id,
                self.gateway.request.repository,
                self.gateway.request.purpose,
            ),
        )

    def test_findings_outside_added_lines_are_dropped(self):
        self.assertEqual(
            [], self._run([{"rule_id": "X", "severity": "high", "path": "a.py", "line": 999}])
        )

    def test_malformed_location_types_are_dropped(self):
        numeric_path_diff = _DIFF.replace("a.py", "1")
        for diff, parsed, path, line in (
            (_DIFF, self.parsed, "a.py", True),
            (_DIFF, self.parsed, "a.py", "1"),
            (_DIFF, self.parsed, "a.py", 1.0),
            (numeric_path_diff, parse_unified_diff(numeric_path_diff), 1, 1),
        ):
            self.gateway.content = json.dumps({"findings": [{"path": path, "line": line}]})
            with self.subTest(path=path, line=line):
                self.assertEqual([], self.reviewer.review_with_context("task-1", diff, parsed))

    def test_missing_findings_array_is_rejected(self):
        self.gateway.content = "{}"
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            self.reviewer.review_with_context("task-1", _DIFF, self.parsed)

    def test_finding_count_is_bounded_before_model_output_normalization(self):
        with self.assertRaisesRegex(RuntimeError, "too many findings"):
            self._run([{}] * (MAX_REVIEWER_FINDINGS + 1))

    def test_unknown_severity_falls_back_to_medium(self):
        findings = self._run([{"rule_id": "X", "severity": "spicy", "path": "a.py", "line": 1}])
        self.assertEqual(Severity.MEDIUM, findings[0].severity)

    def test_confidence_is_clamped(self):
        findings = self._run([{"severity": "low", "path": "a.py", "line": 1, "confidence": 5}])
        self.assertEqual(1.0, findings[0].confidence)

    def test_invalid_confidence_cannot_become_trusted(self):
        for value in (True, "0.9"):
            findings = self._run(
                [{"severity": "high", "path": "a.py", "line": 1, "confidence": value}]
            )
            with self.subTest(value=value):
                self.assertEqual(0.0, findings[0].confidence)

    def test_invalid_text_does_not_bypass_the_shared_finding_boundary(self):
        for changes in (
            {"rule_id": "two words"},
            {"title": ["not", "text"]},
            {"title": "x" * 201},
            {"evidence": {"not": "text"}},
        ):
            finding = {
                "rule_id": "LLM-1",
                "severity": "high",
                "title": "title",
                "path": "a.py",
                "line": 1,
                "evidence": "eval(user_input)",
                **changes,
            }
            with self.subTest(changes=changes):
                self.assertEqual([], self._run([finding]))

    def test_non_json_model_content_is_wrapped(self):
        self.gateway.content = "not json"
        with self.assertRaises(RuntimeError) as ctx:
            self.reviewer.review_with_context("task-1", _DIFF, self.parsed)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_replacement_gateway_cannot_bypass_strict_json(self):
        for content in (
            '{"findings":[],"findings":[]}',
            '{"findings":[],"ignored":NaN}',
            '{"findings":[],"ignored":"\\ud800"}',
            None,
        ):
            self.gateway.content = content
            with (
                self.subTest(content=content),
                self.assertRaisesRegex(RuntimeError, "invalid JSON"),
            ):
                self.reviewer.review_with_context("task-1", _DIFF, self.parsed)


def _finding(rule_id, severity, line=1, path="a.py"):
    return Finding(rule_id, severity, "t", "e", path, line, "ev", "f", "test", 0.9)


class CoordinatorBoundaryTests(unittest.TestCase):
    def test_specialist_pool_does_not_accumulate_after_budget_timeout(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        class SlowReviewer(Reviewer):
            name = "slow-reviewer"

            def review(self, diff, parsed):
                calls.append(1)
                started.set()
                release.wait(2)
                return []

        coordinator = MultiAgentCoordinator([SlowReviewer()], timeout_seconds=0.02)
        captured = Metrics()
        begin = time.monotonic()
        try:
            with (
                mock.patch("evoagent.agents.metrics", captured),
                self.assertRaisesRegex(RuntimeError, "execution budget"),
            ):
                coordinator.review(_DIFF, parse_unified_diff(_DIFF))
            with self.assertRaisesRegex(RuntimeError, "execution budget"):
                coordinator.review(_DIFF, parse_unified_diff(_DIFF))
        finally:
            release.set()

        self.assertTrue(started.is_set())
        self.assertEqual(1, len(calls))
        self.assertLess(time.monotonic() - begin, 1)
        self.assertIn("evoagent_review_agent_budget_timeouts_total 1.0", captured.prometheus())

    def test_context_and_admission_generation_survive_reviewer_wrappers(self):
        class ContextReviewer(Reviewer):
            name = "context-reviewer"

            def __init__(self):
                self.context = None

            def review(self, diff, parsed):
                raise AssertionError("context-free review must not run")

            def review_with_context(self, task_id, diff, parsed, admission_generation=None):
                self.context = (task_id, admission_generation)
                return []

        reviewer = ContextReviewer()
        coordinator = MultiAgentCoordinator(
            [FilteredAgent("filtered-context", reviewer, ("SEC-",))]
        )

        self.assertEqual(
            [], coordinator.review_with_context("task-1", _DIFF, parse_unified_diff(_DIFF), 7)
        )
        self.assertEqual(("task-1", 7), reviewer.context)

    def test_empty_reviewer_graph_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "at least one review agent"):
            MultiAgentCoordinator([])

    def test_reviewer_graph_resource_limits_cannot_be_disabled(self):
        for name, value, message in (
            ("max_workers", 0, "positive integer"),
            ("max_workers", True, "positive integer"),
            ("max_workers", float("nan"), "positive integer"),
            ("timeout_seconds", 0, "positive and finite"),
            ("timeout_seconds", True, "positive and finite"),
            ("timeout_seconds", float("nan"), "positive and finite"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    MultiAgentCoordinator([LocalRuleReviewer()], **{name: value})

    def test_reviewer_graph_size_is_bounded(self):
        class NamedReviewer(Reviewer):
            def __init__(self, index):
                self.name = "reviewer-%d" % index

            def review(self, _diff, _parsed):
                return []

        with self.assertRaisesRegex(ValueError, "at most %d" % MAX_REVIEW_AGENTS):
            MultiAgentCoordinator([NamedReviewer(index) for index in range(MAX_REVIEW_AGENTS + 1)])

    def test_reviewer_names_are_bounded_stable_strings(self):
        class NamedReviewer(Reviewer):
            def __init__(self, name):
                self.name = name

            def review(self, _diff, _parsed):
                return []

        for name in (
            None,
            7,
            "",
            "two words",
            "line\nbreak",
            "x" * (MAX_REVIEW_AGENT_NAME_CHARS + 1),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "agent names"):
                MultiAgentCoordinator([NamedReviewer(name)])

        MultiAgentCoordinator([NamedReviewer("x" * MAX_REVIEW_AGENT_NAME_CHARS)])

    def test_duplicate_reviewer_names_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "review agent names must be unique"):
            MultiAgentCoordinator([LocalRuleReviewer(), LocalRuleReviewer()])

    def test_reviewer_cannot_impersonate_a_coordinator_node(self):
        class ImpostorReviewer(LocalRuleReviewer):
            name = "planner-agent"

        with self.assertRaisesRegex(ValueError, "must not collide with coordinator nodes"):
            MultiAgentCoordinator([ImpostorReviewer()])

    def test_partial_specialist_failure_fails_closed(self):
        class FailedReviewer(LocalRuleReviewer):
            name = "failed-reviewer"

            def review(self, diff, parsed):
                raise RuntimeError("down")

        coordinator = MultiAgentCoordinator([LocalRuleReviewer(), FailedReviewer()])

        with self.assertRaisesRegex(RuntimeError, "review agents failed: failed-reviewer"):
            coordinator.review(_DIFF, parse_unified_diff(_DIFF))

    def test_rejected_agent_message_stops_the_collaboration_graph(self):
        store = mock.Mock()
        store.record_agent_message.return_value = False
        coordinator = MultiAgentCoordinator([LocalRuleReviewer()], store=store)
        coordinator.critic.challenge = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "message persistence was rejected"):
            coordinator.review_with_context("task", _DIFF, parse_unified_diff(_DIFF), 2)

        self.assertEqual(2, store.record_agent_message.call_args.args[-1])
        coordinator.critic.challenge.assert_not_called()

    def test_co_located_claims_keep_independent_critique_results(self):
        valid = Finding(
            "SEC-EVAL",
            Severity.HIGH,
            "Dynamic execution",
            "External input can execute arbitrary code.",
            "a.py",
            1,
            "eval(user_input)",
            "Replace eval with an explicit parser.",
            "Assert hostile input is never executed.",
            0.9,
        )
        invalid = Finding(
            "SEC-EVAL",
            Severity.HIGH,
            "Unsupported claim",
            "This claim quotes evidence absent from the changed line.",
            "a.py",
            1,
            "eval(other_input)",
            "Replace eval with an explicit parser.",
            "Assert hostile input is never executed.",
            0.9,
        )

        class PairReviewer(Reviewer):
            name = "pair"

            def __init__(self, findings):
                self.findings = findings

            def review(self, _diff, _parsed):
                return self.findings

        parsed = parse_unified_diff(_DIFF)
        for findings in ([valid, invalid], [invalid, valid]):
            with self.subTest(first=findings[0].title):
                result = MultiAgentCoordinator([PairReviewer(findings)]).review(_DIFF, parsed)
                self.assertEqual(["eval(user_input)"], [item.evidence for item in result])

    def test_equal_confidence_arbitration_is_order_independent(self):
        first = Finding(
            "SEC-EVAL",
            Severity.HIGH,
            "Dynamic execution",
            "External input can execute arbitrary code.",
            "a.py",
            1,
            "eval(user_input)",
            "Replace eval with an explicit parser.",
            "Assert hostile input is never executed.",
            0.8,
        )
        second = Finding(
            "SEC-EVAL",
            Severity.HIGH,
            "Untrusted execution",
            "User-controlled data can reach dynamic code execution.",
            "a.py",
            1,
            "eval(user_input)",
            "Use an explicit command map instead of eval.",
            "Assert untrusted input is treated only as data.",
            0.8,
        )

        class PairReviewer(Reviewer):
            name = "pair"

            def __init__(self, findings):
                self.findings = findings

            def review(self, _diff, _parsed):
                return self.findings

        parsed = parse_unified_diff(_DIFF)
        selected = []
        for findings in ([first, second], [second, first]):
            result = MultiAgentCoordinator([PairReviewer(findings)]).review(_DIFF, parsed)
            self.assertEqual(1, len(result))
            selected.append(result[0].title)
        self.assertEqual(selected[0], selected[1])

    def test_report_order_is_stable_for_rules_on_the_same_line(self):
        def finding(rule_id):
            return Finding(
                rule_id,
                Severity.HIGH,
                "Dynamic execution",
                "External input can execute arbitrary code.",
                "a.py",
                1,
                "eval(user_input)",
                "Replace eval with an explicit parser.",
                "Assert hostile input is never executed.",
                0.8,
            )

        class PairReviewer(Reviewer):
            name = "pair"

            def __init__(self, findings):
                self.findings = findings

            def review(self, _diff, _parsed):
                return self.findings

        parsed = parse_unified_diff(_DIFF)
        for findings in (
            [finding("SEC-Z"), finding("SEC-A")],
            [finding("SEC-A"), finding("SEC-Z")],
        ):
            with self.subTest(first=findings[0].rule_id):
                result = MultiAgentCoordinator([PairReviewer(findings)]).review(_DIFF, parsed)
                self.assertEqual(["SEC-A", "SEC-Z"], [item.rule_id for item in result])

    def test_empty_evidence_never_reaches_the_report(self):
        class EmptyEvidenceReviewer(Reviewer):
            name = "empty-evidence"

            def __init__(self, evidence):
                self.evidence = evidence

            def review(self, _diff, _parsed):
                return [
                    Finding(
                        "CUSTOM-CHECK",
                        Severity.MEDIUM,
                        "Unsupported claim",
                        "This claim provides no changed-line evidence.",
                        "a.py",
                        1,
                        self.evidence,
                        "Replace the unsafe operation with validated logic.",
                        "Assert hostile input cannot trigger the operation.",
                        0.8,
                    )
                ]

        parsed = parse_unified_diff(_DIFF)
        for evidence in ("", "   "):
            with self.subTest(evidence=repr(evidence)):
                result = MultiAgentCoordinator([EmptyEvidenceReviewer(evidence)]).review(
                    _DIFF, parsed
                )
                self.assertEqual([], result)

    def test_empty_title_never_reaches_the_report(self):
        class EmptyTitleReviewer(Reviewer):
            name = "empty-title"

            def __init__(self, title):
                self.title = title

            def review(self, _diff, _parsed):
                return [
                    Finding(
                        "CUSTOM-CHECK",
                        Severity.MEDIUM,
                        self.title,
                        "External input can execute arbitrary code.",
                        "a.py",
                        1,
                        "eval(user_input)",
                        "Replace eval with an explicit parser.",
                        "Assert hostile input is never executed.",
                        0.8,
                    )
                ]

        parsed = parse_unified_diff(_DIFF)
        for title in ("", "   "):
            with self.subTest(title=repr(title)):
                self.assertEqual(
                    [], MultiAgentCoordinator([EmptyTitleReviewer(title)]).review(_DIFF, parsed)
                )

    def test_invalid_specialist_output_fails_closed_without_polluting_valid_results(self):
        finding = Finding(
            "SEC-EVAL",
            Severity.HIGH,
            "Dynamic execution",
            "External input can execute arbitrary code.",
            "a.py",
            1,
            "eval(user_input)",
            "Replace eval with an explicit parser.",
            "Assert hostile input is never executed.",
            0.9,
        )

        class ValidReviewer(Reviewer):
            name = "valid"

            def review(self, _diff, _parsed):
                return [finding]

        class InvalidReviewer(Reviewer):
            name = "invalid"

            def review(self, _diff, _parsed):
                return [{"rule_id": "not-a-Finding"}]

        coordinator = MultiAgentCoordinator([InvalidReviewer(), ValidReviewer()])
        coordinator.critic.challenge = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "review agents failed: invalid"):
            coordinator.review(_DIFF, parse_unified_diff(_DIFF))

        coordinator.critic.challenge.assert_not_called()

    def test_excessive_specialist_output_never_reaches_downstream_nodes(self):
        class NoisyReviewer:
            name = "noisy"

            def review(self, _diff, _parsed):
                return [_finding("NOISY", Severity.LOW)] * 101

        coordinator = MultiAgentCoordinator([NoisyReviewer()])
        coordinator.critic.challenge = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, "all review agents failed"):
            coordinator.review(_DIFF, parse_unified_diff(_DIFF))

        coordinator.critic.challenge.assert_not_called()

    def test_aggregate_specialist_output_is_bounded_before_downstream_nodes(self):
        class NoisyReviewer:
            def __init__(self, name, prefix):
                self.name = name
                self.prefix = prefix

            def review(self, _diff, _parsed):
                return [_finding(f"{self.prefix}-{index}", Severity.LOW) for index in range(60)]

        coordinator = MultiAgentCoordinator(
            [NoisyReviewer("first", "A"), NoisyReviewer("second", "B")], store=mock.Mock()
        )
        coordinator.store.record_agent_message.return_value = True
        coordinator.critic.challenge = mock.Mock()
        captured = Metrics()

        with (
            mock.patch("evoagent.agents.metrics", captured),
            self.assertRaisesRegex(RuntimeError, "too many findings in aggregate"),
        ):
            coordinator.review_with_context("task-1", _DIFF, parse_unified_diff(_DIFF))

        coordinator.critic.challenge.assert_not_called()
        self.assertEqual(
            ["review_plan"],
            [
                call.args[1]["kind"]
                for call in coordinator.store.record_agent_message.call_args_list
            ],
        )
        self.assertIn(
            "evoagent_review_agent_output_limit_rejections_total 1.0", captured.prometheus()
        )


if __name__ == "__main__":
    unittest.main()
