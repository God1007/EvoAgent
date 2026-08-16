import os
import tempfile
import unittest

from evoagent.policy import RepositoryPolicy, RepositoryPolicyResolver
from evoagent.store import TaskStore


class RepositoryPolicyTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        self.store = TaskStore(self.path)
        self.resolver = RepositoryPolicyResolver(self.store)

    def test_legacy_grants_remain_the_compatibility_fallback(self):
        open_policy = self.resolver.resolve("tenant", "org/initial")
        self.assertTrue(open_policy.enabled)
        self.assertTrue(open_policy.auto_fix)
        self.assertEqual("legacy-grant", open_policy.source)

        self.store.grant_repository("tenant", "org/allowed", auto_fix=False)
        allowed = self.resolver.resolve("tenant", "org/allowed")
        denied = self.resolver.resolve("tenant", "org/other")
        self.assertTrue(allowed.enabled)
        self.assertFalse(allowed.auto_fix)
        self.assertFalse(denied.enabled)

    def test_save_normalizes_versions_and_audits_policy(self):
        first = self.resolver.save(
            "tenant",
            "org/repo",
            {
                "auto_fix": True,
                "allowed_fix_rules": ["SEC-YAML-LOAD", "REL-DEBUG-PRINT"],
                "max_diff_bytes": 2048,
            },
            "alice",
        )
        second = self.resolver.save(
            "tenant",
            "org/repo",
            {"enabled": False, "post_review_comments": False},
            "bob",
        )

        self.assertEqual(1, first["version"])
        self.assertEqual(2, second["version"])
        resolved = self.resolver.resolve("tenant", "org/repo")
        self.assertFalse(resolved.enabled)
        self.assertFalse(resolved.post_review_comments)
        self.assertEqual(2, resolved.version)
        history = self.store.list_repository_policy_versions("tenant", "org/repo")
        self.assertEqual(["bob", "alice"], [item["actor"] for item in history])
        audit = self.store.list_audit("tenant", 10)
        self.assertEqual(
            2, len([item for item in audit if item["action"] == "repository-policy.updated"])
        )

    def test_policy_rejects_unknown_fields_and_ambiguous_types(self):
        for value, message in (
            ({"enabled": 1}, "boolean"),
            ({"max_diff_bytes": True}, "positive integer"),
            ({"allowed_reviewers": ["same", "same"]}, "duplicates"),
            ({"unknown": True}, "unsupported"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                RepositoryPolicy.from_dict(value)

    def test_review_authorization_enforces_each_configured_dimension(self):
        baseline = RepositoryPolicy.from_dict(
            {
                "allowed_reviewers": ["reviewer-a"],
                "allowed_llm_providers": ["local"],
                "allowed_llm_models": ["model-a"],
                "max_diff_bytes": 100,
            }
        )
        self.resolver.authorize_review(baseline, 100, "reviewer-a", "local", "model-a")
        cases = (
            (101, "reviewer-a", "local", "model-a", ValueError),
            (10, "reviewer-b", "local", "model-a", PermissionError),
            (10, "reviewer-a", "remote", "model-a", PermissionError),
            (10, "reviewer-a", "local", "model-b", PermissionError),
        )
        for size, reviewer, provider, model, error in cases:
            with self.subTest(reviewer=reviewer, provider=provider, model=model):
                with self.assertRaises(error):
                    self.resolver.authorize_review(baseline, size, reviewer, provider, model)

    def test_fix_authorization_returns_only_policy_allowlist(self):
        policy = RepositoryPolicy.from_dict(
            {"auto_fix": True, "allowed_fix_rules": ["SEC-YAML-LOAD"]}
        )
        self.assertEqual(
            ("SEC-YAML-LOAD",),
            self.resolver.authorize_fix(policy, ("SEC-YAML-LOAD", "REL-DEBUG-PRINT")),
        )
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.resolver.authorize_fix(policy, ("REL-DEBUG-PRINT",))

    def test_task_snapshot_round_trip_is_immutable_decision_input(self):
        configured = RepositoryPolicy.from_dict({"max_diff_bytes": 42})
        configured = configured.__class__(
            **{**configured.__dict__, "version": 7, "source": "configured"}
        )
        restored = self.resolver.from_snapshot(self.resolver.snapshot(configured))
        self.assertEqual(7, restored.version)
        self.assertEqual(42, restored.max_diff_bytes)
        self.assertEqual("task-snapshot", restored.source)


if __name__ == "__main__":
    unittest.main()
