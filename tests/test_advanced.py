import os
import tempfile
import time
import unittest

from evoagent.config import Settings
from evoagent.evolution import EvolutionEngine, RegressionEvaluator
from evoagent.fixer import SafeFixer
from evoagent.models import Finding, Severity
from evoagent.service import ReviewService
from evoagent.store import TaskStore


def settings(path):
    return Settings(
        host="127.0.0.1", port=8080, db_path=path, max_diff_bytes=10000,
        max_steps=8, timeout_seconds=10, llm_base_url="", llm_api_key="", llm_model="",
        github_webhook_secret="", github_token="", auto_post_review=False,
        skills_dir="skills",
    )


class AdvancedFeatureTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)

    def tearDown(self):
        os.unlink(self.path)

    def test_safe_fixer_changes_only_supported_rules(self):
        content = 'password = "secret-value"\nresult = eval(user_input)\nprint(result)\n'
        findings = [
            {"path": "app.py", "line": 1, "rule_id": "SEC-HARDCODED-SECRET"},
            {"path": "app.py", "line": 2, "rule_id": "SEC-EVAL"},
            {"path": "app.py", "line": 3, "rule_id": "REL-DEBUG-PRINT"},
        ]
        result = SafeFixer().apply(content, findings, "app.py")
        self.assertIn("import os", result["content"])
        self.assertIn('password = os.environ["PASSWORD"]', result["content"])
        self.assertIn("eval(user_input)", result["content"])
        self.assertNotIn("print(result)", result["content"])
        self.assertEqual({"SEC-HARDCODED-SECRET", "REL-DEBUG-PRINT"}, set(result["rules"]))

    def test_deepseek_and_free_openrouter_provider_presets(self):
        deepseek = settings(self.path)
        deepseek = deepseek.__class__(
            **{**deepseek.__dict__, "llm_provider": "deepseek", "deepseek_api_key": "test-key"}
        )
        self.assertEqual("https://api.deepseek.com", deepseek.resolved_llm()["base_url"])
        self.assertEqual("deepseek-v4-flash", deepseek.resolved_llm()["model"])

        free = settings(self.path)
        free = free.__class__(
            **{
                **free.__dict__,
                "llm_provider": "openrouter-deepseek-free",
                "openrouter_api_key": "test-key",
            }
        )
        self.assertTrue(str(free.resolved_llm()["model"]).endswith(":free"))

    def test_feedback_candidate_is_deferred_without_a_model(self):
        store = TaskStore(self.path)
        store.create("task", "org/repo", 1, {"source": "test"})
        store.record_failure_case("task", "false_positive", {"note": "style-only"})
        engine = EvolutionEngine(store)
        result = engine.auto_propose("llm-review")
        self.assertEqual("deferred", result["decision"])
        self.assertEqual(1, result["failure_cases_used"])
        version = result["version"]["version"]
        self.assertTrue(engine.rollback("llm-review", version))

    def test_replay_evaluation_activates_only_an_improved_prompt(self):
        store = TaskStore(self.path)
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        store.save_evaluation_case(
            "eval-case", "validation", diff,
            [{"path": "a.py", "line": 1, "min_severity": "high"}], "test",
        )

        class PromptAwareReviewer:
            def __init__(self, prompt):
                self.prompt = prompt

            def review(self, _diff, parsed):
                if "improved" not in self.prompt:
                    return []
                line = parsed.added_lines[0]
                return [Finding(
                    "SEC-EVAL", Severity.CRITICAL, "eval", "danger", line.path, line.line,
                    line.content, "replace it", "add a test", 0.9,
                )]

        engine = EvolutionEngine(
            store, reviewer_factory=PromptAwareReviewer, min_cases=1,
            max_cases=1, min_improvement=0.01, seed_defaults=False,
        )
        result = engine.propose(
            "llm-review",
            "improved: Review the diff and return JSON with severity, fix and test.",
            regression_score=0.0,
        )
        self.assertEqual("activated", result["decision"])
        self.assertGreater(result["candidate"]["score"], result["baseline"]["score"])
        self.assertTrue(result["version"]["active"])
        self.assertTrue(
            store.list_evolution_runs()[0]["metrics"]["external_regression_score_ignored"]
        )
        rejected = engine.propose(
            "llm-review",
            "Review the diff and return JSON with severity, fix and test.",
        )
        self.assertEqual("rejected", rejected["decision"])
        self.assertEqual(
            result["version"]["version"],
            store.get_active_skill_version("llm-review")["version"],
        )

    def test_evaluation_errors_are_counted_as_misses_and_reduce_score(self):
        positive = {
            "id": 1,
            "name": "positive",
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n",
            "expected": [{"path": "a.py", "line": 1, "min_severity": "high"}],
        }
        clean = {
            "id": 2,
            "name": "clean",
            "diff": "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old\n+value = int(raw)\n",
            "expected": [],
        }

        class BrokenReviewer:
            def review(self, _diff, _parsed):
                raise RuntimeError("provider unavailable")

        metrics = RegressionEvaluator(lambda _prompt: BrokenReviewer()).run(
            "prompt", [positive, clean]
        )
        self.assertEqual(0.0, metrics["score"])
        self.assertEqual(0.0, metrics["recall"])
        self.assertEqual(0.0, metrics["clean_accuracy"])
        self.assertEqual(0.0, metrics["success_rate"])
        self.assertEqual(2, len(metrics["errors"]))

    def test_rule_id_matching_rejects_wrong_finding_on_the_same_line(self):
        case = {
            "id": 1,
            "name": "semantic-match",
            "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n",
            "expected": [{
                "path": "a.py", "line": 1, "rule_id": "SEC-EVAL",
                "min_severity": "high",
            }],
        }

        class WrongRuleReviewer:
            def review(self, _diff, parsed):
                line = parsed.added_lines[0]
                return [Finding(
                    "REL-DEBUG-PRINT", Severity.CRITICAL, "wrong", "wrong category",
                    line.path, line.line, line.content, "fix", "test", 0.9,
                )]

        metrics = RegressionEvaluator(lambda _prompt: WrongRuleReviewer()).run(
            "prompt", [case]
        )
        self.assertEqual(0, metrics["case_results"][0]["tp"])
        self.assertEqual(1, metrics["case_results"][0]["fp"])
        self.assertEqual(1, metrics["case_results"][0]["fn"])

    def test_holdout_regression_blocks_activation_without_leaking_case_details(self):
        store = TaskStore(self.path)
        validation_diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        holdout_diff = "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-old\n+safe_call(data)\n"
        store.save_evaluation_case(
            "validation-positive", "validation", validation_diff,
            [{"path": "a.py", "line": 1, "min_severity": "high"}], "test",
        )
        store.save_evaluation_case(
            "secret-holdout-clean", "holdout", holdout_diff, [], "test",
        )

        class HoldoutAwareReviewer:
            def __init__(self, prompt):
                self.prompt = prompt

            def review(self, diff, parsed):
                line = parsed.added_lines[0]
                if "eval(data)" in diff and "candidate" in self.prompt:
                    return [Finding(
                        "SEC-EVAL", Severity.CRITICAL, "eval", "danger", line.path,
                        line.line, line.content, "fix", "test", 0.9,
                    )]
                if "safe_call(data)" in diff and "candidate" in self.prompt:
                    return [Finding(
                        "FAKE", Severity.HIGH, "false positive", "not a defect", line.path,
                        line.line, line.content, "fix", "test", 0.9,
                    )]
                return []

        engine = EvolutionEngine(
            store, reviewer_factory=HoldoutAwareReviewer, min_cases=1, max_cases=2,
            min_holdout_cases=1, seed_defaults=False,
        )
        result = engine.propose(
            "llm-review",
            "candidate: Review the diff and return JSON with severity, fix and test.",
        )
        self.assertEqual("rejected", result["decision"])
        self.assertIn("holdout", result["reason"])
        self.assertNotIn("case_results", result["candidate_holdout"])
        persisted = store.list_evolution_runs()[0]["metrics"]
        self.assertNotIn("secret-holdout-clean", str(persisted))
        self.assertEqual(1, persisted["candidate_holdout"]["clean_cases"])

    def test_auto_evolution_does_not_create_duplicate_noop_versions(self):
        store = TaskStore(self.path)
        engine = EvolutionEngine(store, seed_defaults=False)
        result = engine.auto_propose("llm-review")
        self.assertEqual("deferred", result["decision"])
        self.assertIsNone(result["version"])
        self.assertEqual([], store.list_skill_versions("llm-review"))

    def test_evaluation_cases_are_immutable_and_idempotent(self):
        store = TaskStore(self.path)
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        expected = [{"path": "a.py", "line": 1, "min_severity": "high"}]
        first = store.save_evaluation_case(
            "stable-case-v1", "validation", diff, expected, "test"
        )
        repeated = store.save_evaluation_case(
            "stable-case-v1", "validation", diff, expected, "test"
        )
        self.assertEqual(first["id"], repeated["id"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            store.save_evaluation_case(
                "stable-case-v1", "validation", diff, [], "test"
            )

    def test_async_multi_agent_review(self):
        service = ReviewService(settings(self.path))
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n-old\n+eval(data)\n+# TODO finish validation\n"
        result = service.enqueue_review("org/repo", diff, 2)
        task = None
        for _ in range(50):
            task = service.store.get(result["task_id"])
            if task["state"] in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.02)
        service.queue.close()
        self.assertEqual("SUCCESS", task["state"])
        self.assertEqual({"SEC-EVAL", "QUALITY-UNFINISHED"},
                         {item["rule_id"] for item in task["report"]["findings"]})


if __name__ == "__main__":
    unittest.main()
