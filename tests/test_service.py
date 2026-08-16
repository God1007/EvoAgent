import os
import tempfile
import unittest
from dataclasses import dataclass

from evoagent.config import Settings
from evoagent.models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from evoagent.service import ReviewService
from evoagent.store import utc_now


@dataclass
class _Report:
    findings: list


def _finding(**overrides):
    base = dict(
        rule_id="SEC-EVAL",
        severity=Severity.HIGH,
        title="Dangerous eval",
        explanation="e",
        path="app/service.py",
        line=10,
        evidence="eval(user_input)",
        fix="f",
        test="t",
    )
    base.update(overrides)
    return Finding(**base)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.settings = Settings(
            host="127.0.0.1",
            port=8080,
            db_path=self.path,
            max_diff_bytes=10000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )

    def test_fix_publication_result_is_cached_by_durable_effect_key(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.close)
        task_id = "fix-task"
        service.store.create(task_id, "org/repo", 7, {}, "default")
        service.store.succeed(
            task_id,
            ReviewReport("org/repo", 7, "one issue", "high", [_finding()]),
            TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
        )

        class CountingFixer:
            calls = 0

            def create_fix_commits(self, *_args, **_kwargs):
                self.calls += 1
                return {"branch": "evoagent/fix", "commits": [{"sha": "abc"}]}

        fixer = CountingFixer()
        service.fixer = fixer
        service.github_client_for_installation = lambda _installation=None: service.github

        first = service.create_fix(task_id)
        second = service.create_fix(task_id)

        self.assertEqual(first, second)
        self.assertEqual(1, fixer.calls)

    def tearDown(self):
        os.unlink(self.path)

    def test_end_to_end_review(self):
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        result = ReviewService(self.settings).create_review("org/repo", diff, 1)
        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual("SEC-EVAL", result["report"]["findings"][0]["rule_id"])

    def test_rejects_large_diff(self):
        service = ReviewService(self.settings)
        with self.assertRaises(ValueError):
            service.create_review("org/repo", "x" * 10001)


class ServiceSessionTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        self.service = ReviewService(
            Settings(
                host="127.0.0.1",
                port=8080,
                db_path=self.path,
                max_diff_bytes=10000,
                max_steps=8,
                timeout_seconds=10,
                llm_base_url="",
                llm_api_key="",
                llm_model="",
                github_webhook_secret="",
                github_token="",
                auto_post_review=False,
            )
        )

    def _turn(self, head_sha, trigger, findings):
        turn = self.service.store.start_session_turn("default", "org/repo", 7, head_sha, trigger)
        payload = {
            "repository": "org/repo",
            "task_id": "task-%s" % head_sha,
            "session_id": turn["session_id"],
            "turn_id": turn["turn_id"],
            "head_sha": head_sha,
        }
        note = self.service._record_session_turn(payload, _Report(findings))
        return turn, note

    def test_first_turn_produces_no_continuity_note(self):
        _, note = self._turn("sha1", "opened", [_finding()])
        self.assertEqual("", note)

    def test_second_turn_reports_resolved_and_still_open(self):
        self._turn(
            "sha1",
            "opened",
            [
                _finding(rule_id="SEC-EVAL", evidence="eval(x)"),
                _finding(rule_id="REL-DEBUG-PRINT", evidence="print(x)"),
            ],
        )
        _, note = self._turn(
            "sha2",
            "synchronize",
            [
                _finding(rule_id="SEC-EVAL", evidence="eval(x)"),
                _finding(rule_id="SEC-HARDCODED-SECRET", evidence="token='abc'"),
            ],
        )
        self.assertIn("新增 1", note)
        self.assertIn("仍存在 1", note)
        self.assertIn("已修复 1", note)

    def test_timeline_backing_method_returns_turns(self):
        turn, _ = self._turn("sha1", "opened", [_finding()])
        timeline = self.service.get_session_timeline(turn["session_id"])
        self.assertEqual(1, len(timeline["turns"]))
        by_pr = self.service.get_session_for_pull_request("org/repo", 7)
        self.assertEqual(turn["session_id"], by_pr["id"])

    def test_provide_session_input_reopens_session(self):
        turn, _ = self._turn("sha1", "opened", [_finding()])
        self.service.store.set_session_input_required(turn["session_id"], "which env?")
        result = self.service.provide_session_input(turn["session_id"], "prod", "default")
        self.assertEqual("open", result["status"])
        session = self.service.store.get_session("default", "org/repo", 7)
        self.assertEqual("open", session["status"])

    def test_provide_input_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            self.service.provide_session_input("00000000-0000-0000-0000-000000000000", "x")

    def test_analyze_impact_reports_blast_radius(self):
        sources = {
            "pkg/util.py": "def helper():\n    return 1\n",
            "pkg/service.py": (
                "from pkg import util\ndef use_helper():\n    return util.helper()\n"
            ),
        }
        result = self.service.analyze_impact(sources, ["pkg/util.py"])
        self.assertIn("pkg.util.helper", result["changed_symbols"])
        self.assertIn("pkg.service.use_helper", result["impacted_symbols"])

    def test_analyze_impact_validates_payload(self):
        with self.assertRaises(ValueError):
            self.service.analyze_impact("not-a-dict", [])

    def test_run_proof_without_reproduction_is_l1(self):
        result = self.service.run_proof({"a.py": "x=1\n"}, {"a.py": "x=2\n"})
        self.assertEqual(1, result["evidence_level"])
        self.assertIn("+x=2", result["patch"])

    def test_run_proof_validates_payload(self):
        with self.assertRaises(ValueError):
            self.service.run_proof("nope", {})


if __name__ == "__main__":
    unittest.main()
