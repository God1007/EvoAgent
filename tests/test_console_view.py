"""Transport projections omit internal fields, including future/unknown payloads."""

import copy
import json
import unittest

from evoagent.console_view import ERROR_CODES, console_error, console_response
from evoagent.errors import ClientInputError


class ConsoleViewTests(unittest.TestCase):
    def test_repository_policy_view_is_human_safe_and_field_allowlisted(self):
        payload = {
            "tenant_id": "internal-tenant",
            "repository": "org/repo",
            "version": 3,
            "source": "configured",
            "updated_at": "2026-08-30T00:00:00+00:00",
            "policy": {
                "enabled": True,
                "auto_fix": False,
                "post_review_comments": True,
                "allowed_reviewers": ["review"],
                "allowed_fix_rules": ["SEC-YAML-LOAD"],
                "allowed_llm_providers": [],
                "allowed_llm_models": [],
                "llm_region": None,
                "max_diff_bytes": 4096,
                "credential": "internal-policy-secret",
            },
            "history": [
                {
                    "tenant_id": "internal-tenant",
                    "version": 3,
                    "actor": "alice",
                    "created_at": "2026-08-30T00:00:00+00:00",
                    "policy": {"credential": "internal-history-secret"},
                }
            ],
            "available_reviewers": ["review", {"credential": "internal"}],
            "available_fix_rules": ["SEC-YAML-LOAD", None],
        }

        for method in ("GET", "POST"):
            with self.subTest(method=method):
                view = console_response(method, "/v1/repository-policies", payload)
                self.assertEqual("org/repo", view["repository"])
                self.assertEqual(["review"], view["available_reviewers"])
                self.assertEqual(["SEC-YAML-LOAD"], view["available_fix_rules"])
                self.assertEqual(
                    {"version": 3, "actor": "alice", "created_at": "2026-08-30T00:00:00+00:00"},
                    view["history"][0],
                )
                self.assertNotIn("internal", json.dumps(view))

    def test_operational_views_expose_actions_without_payloads_or_error_text(self):
        audit = console_response(
            "GET",
            "/api/audit",
            {
                "events": [
                    {
                        "actor": "alice",
                        "action": "task.resume",
                        "resource": "task-1",
                        "created_at": "2026-08-30T00:00:00+00:00",
                        "detail": {"credential": "internal-secret"},
                    }
                ]
            },
        )
        outbox = console_response(
            "GET",
            "/api/outbox",
            {
                "messages": [
                    {
                        "id": "review:task-1",
                        "status": "dead",
                        "attempts": 5,
                        "updated_at": "2026-08-30T00:00:00+00:00",
                        "last_error": "operation [type=network; ref=internal-ref]",
                        "payload": {"token": "internal-token"},
                    }
                ]
            },
        )
        dead = console_response(
            "GET",
            "/api/queue/dead-letters",
            {
                "messages": [
                    {
                        "message_id": "internal-message",
                        "attempt": 3,
                        "failed_at": 1.5,
                        "error": "operation [type=contract; ref=internal-ref]",
                        "payload": {"task_id": "task-1", "credential": "internal-token"},
                    }
                ]
            },
        )
        capacity = console_response(
            "GET",
            "/api/tenant-review-capacity",
            {
                "tenant_id": "internal-tenant",
                "enabled": True,
                "max_active_reviews": 4,
                "active_reviews": 2,
                "available": 2,
                "saturated": False,
                "oldest_acquired_at": None,
            },
        )

        self.assertEqual("task.resume", audit["events"][0]["action"])
        self.assertEqual(True, outbox["messages"][0]["error"])
        self.assertEqual("task-1", dead["messages"][0]["task_id"])
        self.assertEqual(2, capacity["available"])
        self.assertTrue(
            all("internal" not in json.dumps(view) for view in (audit, outbox, dead, capacity))
        )
        self.assertEqual(
            {"replayed": True},
            console_response(
                "POST", "/v1/outbox/replay", {"replayed": True, "payload": "internal"}
            ),
        )

    def test_workflow_view_exposes_readable_duration_without_internal_timing_metadata(self):
        payload = {
            "availability": "recorded",
            "task_state": "SUCCESS",
            "workflow": {"name": "review", "revision": "internal-revision"},
            "steps": [
                {
                    "id": "review",
                    "inputs": {},
                    "outputs": {},
                    "sources": {},
                    "status": "completed",
                    "blocked_by": [],
                    "attempt": 1,
                    "started_at": "internal-start",
                    "duration_ms": 1250,
                    "updated_at": "2026-08-29T00:00:00+00:00",
                    "input_sha256": "internal-input",
                }
            ],
        }

        view = console_response("GET", "/v1/tasks/abc/workflow", payload)

        self.assertEqual(1250, view["steps"][0]["duration_ms"])
        self.assertNotIn("started_at", view["steps"][0])
        self.assertNotIn("internal", json.dumps(view))

    def test_proof_view_keeps_evidence_but_omits_attestations_and_infrastructure_detail(self):
        result = {
            "evidence_level": 2,
            "evidence_label": "L2-reproduced",
            "note": "internal note",
            "patch": "-old\n+new",
            "steps": [
                {
                    "step": "reproduce-on-original",
                    "status": "failed",
                    "detail": "assertion",
                    "duration_seconds": 1,
                    "attestation": {"request_sha256": "internal-hash"},
                },
                {"step": "reproduce-on-patched", "status": "error", "detail": "internal-ref"},
            ],
        }
        view = console_response("POST", "/v1/proofs", result)
        self.assertEqual(2, view["evidence_level"])
        self.assertEqual("-old\n+new", view["patch"])
        self.assertEqual("assertion", view["steps"][0]["detail"])
        self.assertEqual("", view["steps"][1]["detail"])
        self.assertNotIn("internal", json.dumps(view))

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
            "skills": [
                {
                    "id": "diff-summary",
                    "version": 1,
                    "name": "Diff summary",
                    "description": "Summarize the connected diff",
                    "mode": "tool",
                    "requires": ["unified-diff@1"],
                    "instructions": "internal-skill-instructions",
                }
            ],
            "builtins": [{"id": "planner", "version": 0, "revision": "internal-revision"}],
            "models": [{"model": "demo", "provider": "local", "region": "internal-region"}],
            "agent_recipes": [
                {
                    "id": "feedback-loop",
                    "name": "Regression",
                    "description": "Find missing verification",
                    "internal": "internal-recipe",
                    "definition": {
                        "name": "Regression",
                        "kind": "llm",
                        "inputs": {"diff": "unified-diff@1"},
                        "outputs": {"findings": "review-findings@1"},
                        "internal": "internal-definition",
                        "config": {
                            "playbook": {
                                "identity": "Reviewer",
                                "objective": "Check tests",
                                "instructions": "Use public seams",
                                "internal": "internal-playbook",
                            },
                            "model": "demo",
                            "skills": [{"id": "diff-summary", "version": 1}],
                            "max_output_tokens": 100,
                            "credential": "internal-credential",
                        },
                    },
                }
            ],
            "templates": [
                {
                    "id": "dual-axis-review",
                    "name": "Dual axis",
                    "description": "Standards and spec",
                    "internal": "internal-template",
                    "definition": {
                        "name": "Dual axis",
                        "steps": [
                            {
                                "id": "spec",
                                "agent": "spec-review",
                                "version": 0,
                                "sources": {"context": "$input.context"},
                                "prompt": "internal-prompt",
                            }
                        ],
                        "outputs": {"verified": "spec.findings"},
                        "digest": "internal-digest",
                    },
                }
            ],
        }
        view = console_response("GET", "/v1/studio/catalog", catalog)
        self.assertNotIn("internal-", json.dumps(view))
        self.assertEqual([{"model": "demo", "provider": "local"}], view["models"])
        self.assertEqual(
            [
                {
                    "id": "diff-summary",
                    "version": 1,
                    "name": "Diff summary",
                    "description": "Summarize the connected diff",
                    "mode": "tool",
                    "requires": ["unified-diff@1"],
                }
            ],
            view["skills"],
        )
        self.assertEqual(
            "Check tests",
            view["agent_recipes"][0]["definition"]["config"]["playbook"]["objective"],
        )
        self.assertEqual(
            "$input.context", view["templates"][0]["definition"]["steps"][0]["sources"]["context"]
        )

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
            "context": (
                "review-context@1",
                {
                    "origin": "api",
                    "title": "Review",
                    "spec": "Expected behavior",
                    "standards": "Project rules",
                    "truncated": False,
                    "internal-metadata": "private",
                },
            ),
            "evidence": (
                "repository-evidence@1",
                {
                    "origin": "github-archive",
                    "status": "available",
                    "revision": "a" * 40,
                    "indexed_files": 2,
                    "indexed_bytes": 100,
                    "changed_paths": ["app.py"],
                    "changed_symbols": ["app.run"],
                    "impacted_symbols": ["worker.call"],
                    "importing_files": ["worker.py"],
                    "truncated": False,
                    "internal-metadata": "private",
                },
            ),
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
        self.assertEqual(
            {
                "origin": "api",
                "title": "Review",
                "spec": "Expected behavior",
                "standards": "Project rules",
                "truncated": False,
            },
            view["outputs"]["context"],
        )
        self.assertEqual(
            {
                "origin": "github-archive",
                "status": "available",
                "revision": "a" * 40,
                "indexed_files": 2,
                "indexed_bytes": 100,
                "changed_paths": ["app.py"],
                "changed_symbols": ["app.run"],
                "impacted_symbols": ["worker.call"],
                "importing_files": ["worker.py"],
                "truncated": False,
            },
            view["outputs"]["evidence"],
        )
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
