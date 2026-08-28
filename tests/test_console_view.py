"""Transport projections omit internal fields, including future/unknown payloads."""

import copy
import json
import unittest

from evoagent.console_view import ERROR_CODES, console_error, console_response
from evoagent.errors import ClientInputError


class ConsoleViewTests(unittest.TestCase):
    def test_error_projection_never_copies_arbitrary_fields_or_unknown_messages(self):
        for message, code in ERROR_CODES.items():
            self.assertEqual({"error_code": code}, console_error(400, {"error": message}))
        for message in ("private-token", {"credential": "private"}, ["private"], None):
            payload = {"error": message, "detail": "private", "request_id": "private"}
            before = copy.deepcopy(payload)
            self.assertEqual({"error_code": "access_denied"}, console_error(403, payload))
            self.assertEqual(before, payload)
        self.assertEqual(
            {"error_code": "internal_error"},
            console_error(500, {"error": "diff is required"}),
        )

    def test_task_controls_expose_state_hints_without_internal_delivery_metadata(self):
        for state, retrying, cancelled, remote, complete, expected in (
            ("PENDING", False, False, False, False, (True, False, False)),
            ("EXECUTING", False, False, False, False, (True, False, False)),
            ("EXECUTING", False, True, False, False, (False, False, False)),
            ("FAILED", False, False, False, False, (False, True, False)),
            ("FAILED", True, False, False, False, (True, False, False)),
            ("CANCELLED", False, True, False, False, (False, False, False)),
            ("SUCCESS", False, False, False, False, (False, False, False)),
            ("SUCCESS", False, False, True, False, (False, True, True)),
            ("SUCCESS", False, False, True, True, (False, False, False)),
        ):
            with self.subTest(state=state, retrying=retrying, cancelled=cancelled, remote=remote):
                task = {
                    "state": state,
                    "retrying": retrying,
                    "cancel_requested": cancelled,
                    "input": {
                        "session_id": "internal-session" if remote else None,
                        "_delivery_complete": complete,
                        "_delivery_resume_outbox_id": "internal-outbox",
                    },
                }
                before = copy.deepcopy(task)
                view = console_response("GET", "/v1/tasks/abc", task)
                self.assertEqual(
                    expected,
                    tuple(view[key] for key in ("can_cancel", "can_resume", "delivery_pending")),
                )
                self.assertEqual(cancelled, view["cancel_requested"])
                self.assertNotIn("internal-", json.dumps(view))
                self.assertEqual(before, task)
        response = console_response(
            "POST",
            "/v1/tasks/abc/resume",
            {
                "task_id": "abc",
                "state": "SUCCESS",
                "delivery_resumed": False,
                "delivery_already_active": True,
                "report": {"internal-report": True},
            },
        )
        self.assertTrue(response["delivery_already_active"])
        self.assertNotIn("report", response)

    def test_task_and_catalog_omit_nested_runtime_metadata(self):
        task = {
            "id": "abc",
            "state": "SUCCESS",
            "fix_blocker": "",
            "repository": "demo/repo",
            "pull_request": 1,
            "trace": "internal-trace",
            "input": {
                "head_sha": "internal-head",
                "studio_workflow": {
                    "version": 1,
                    "digest": "internal-bundle-digest",
                    "bundle": {
                        "definition": {
                            "name": "Team review",
                            "steps": [{"id": "review", "agent": "abc", "version": 1}],
                        },
                        "agents": {
                            "abc_v1": {"name": "Security", "config": {"prompt": "internal-prompt"}}
                        },
                    },
                },
            },
            "report": {
                "risk": "high",
                "future_metadata": "internal-report-field",
                "findings": [{"title": "Check input", "fingerprint": "internal-fingerprint"}],
            },
        }
        before = copy.deepcopy(task)
        view = console_response("GET", "/v1/tasks/abc", task)
        self.assertTrue(view["can_fix"])
        self.assertEqual({"risk": "high", "findings": [{"title": "Check input"}]}, view["report"])
        self.assertEqual(
            {"version": 1, "name": "Team review", "steps": {"review": "Security"}}, view["workflow"]
        )
        self.assertNotIn("internal-", json.dumps(view))
        self.assertEqual(before, task)
        for blocker in (None, {"credential": "private"}, "private-error", "sandbox"):
            with self.subTest(blocker=blocker):
                blocked = console_response("GET", "/v1/tasks/abc", {**task, "fix_blocker": blocker})
                self.assertFalse(blocked["can_fix"])
                self.assertNotIn("private", json.dumps(blocked))
        del task["report"]["findings"]
        self.assertNotIn("findings", console_response("GET", "/v1/tasks/abc", task)["report"])
        self.assertFalse(console_response("GET", "/v1/tasks/abc", {"state": "FAILED"})["can_fix"])

        catalog = {
            "rules": [{"id": "check", "title": "Check input", "future": "internal-rule"}],
            "builtins": [{"id": "planner", "version": 0, "revision": "internal-revision"}],
            "models": [{"model": "demo", "provider": "local", "region": "internal-region"}],
        }
        view = console_response("GET", "/v1/studio/catalog", catalog)
        self.assertNotIn("internal-", json.dumps(view))
        self.assertEqual([{"model": "demo", "provider": "local"}], view["models"])

    def test_artifact_types_are_explicit_and_never_dump_unknown_structures(self):
        values = {
            "diff": ("unified-diff@1", "+value = 1"),
            "parsed": (
                "parsed-diff@1",
                {"files": ["app.py"], "added_lines": [{"content": "internal-line-body"}]},
            ),
            "plan": (
                "review-plan@1",
                {
                    "languages": ["python"],
                    "changed_files": ["app.py"],
                    "assignments": [{"agent": "internal-assignment"}],
                },
            ),
            "critique": (
                "review-critiques@1",
                {"internal-finding-key": {"accepted": True, "internal-field": "private"}},
            ),
            "test": ("review-reproductions@1", {"internal-finding-key": {"reproducible": False}}),
            "fix": ("review-fix-decisions@1", {"internal-finding-key": True}),
            "text": ("text@1", "human summary"),
            "integer": ("integer@1", 3),
            "bool": ("boolean@1", True),
            "unknown": ("private-schema@1", {"internal-prompt": "private"}),
            "findings": (
                "review-findings@1",
                [
                    {
                        "title": "human issue",
                        "fingerprint": "internal-digest",
                        "internal-future-field": "private",
                    }
                ],
            ),
        }
        payload = {
            "step_id": "review",
            "status": "completed",
            "output_sha256": "internal-output-digest",
            "inputs": {},
            "outputs": {key: value for key, (_, value) in values.items()},
            "port_types": {
                "inputs": {},
                "outputs": {key: kind for key, (kind, _) in values.items()},
            },
        }
        before = copy.deepcopy(payload)
        view = console_response("GET", "/v1/tasks/abc/workflow/review", payload)
        self.assertNotIn("internal-", json.dumps(view))
        self.assertIsNone(view["outputs"]["unknown"])
        self.assertEqual({"checked": 1, "accepted": 1}, view["outputs"]["critique"])
        self.assertEqual({"checked": 1, "accepted": 0}, view["outputs"]["test"])
        self.assertEqual({"checked": 1, "accepted": 1}, view["outputs"]["fix"])
        self.assertEqual(1, view["outputs"]["parsed"]["added_line_count"])
        self.assertEqual(1, view["outputs"]["plan"]["assignment_count"])
        self.assertEqual(before, payload)
        payload["status"] = "failed"
        self.assertEqual(
            {}, console_response("GET", "/v1/tasks/abc/workflow/review", payload)["outputs"]
        )
        payload["port_types"] = {}
        self.assertEqual(
            {}, console_response("GET", "/v1/tasks/abc/workflow/review", payload)["outputs"]
        )
        with self.assertRaises(ClientInputError):
            console_response("GET", "/api/unknown")


if __name__ == "__main__":
    unittest.main()
