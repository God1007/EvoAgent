import unittest
from unittest import mock

from evoagent.application import (
    GitHubInstallationUseCases,
    PolicyUseCases,
    RepairOptions,
    RepairUseCases,
    ReviewOptions,
    ReviewUseCases,
    SessionUseCases,
    WebhookOptions,
    WebhookUseCases,
)
from evoagent.auth import AuthManager, Principal
from evoagent.errors import (
    AccessDeniedError,
    ClientInputError,
    ResourceNotFoundError,
    StateConflictError,
)
from evoagent.metrics import Metrics
from evoagent.models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from evoagent.observability import AlertManager, Observability
from evoagent.policy import RepositoryPolicy, RepositoryPolicyResolver
from evoagent.rollout import ReleaseManager
from evoagent.task_queue import PermanentTaskError
from evoagent.time_utils import utc_now
from tests.db_support import postgres_store


def _finding() -> Finding:
    return Finding(
        rule_id="SEC-EVAL",
        severity=Severity.HIGH,
        title="Dangerous eval",
        explanation="Untrusted code may execute.",
        path="app.py",
        line=1,
        evidence="eval(value)",
        fix="Use a safe parser.",
        test="Pass an expression as data.",
    )


class PolicyUseCaseBoundaryTests(unittest.TestCase):
    def test_unavailable_reviewer_is_rejected_before_policy_persistence(self):
        policies = mock.Mock()
        use_cases = PolicyUseCases(
            mock.Mock(),
            policies,
            lambda: (),
            lambda: ("multi-agent-collaboration",),
        )

        with self.assertRaisesRegex(ClientInputError, "unavailable reviewers"):
            use_cases.set_repository_policy(
                "tenant",
                "org/repo",
                {"allowed_reviewers": ["missing-pipeline"]},
                "alice",
            )

        policies.save.assert_not_called()


class GitHubInstallationBindingTests(unittest.TestCase):
    def test_installation_id_does_not_use_numeric_coercion(self):
        auth = mock.Mock()
        oauth = mock.Mock(
            client_id="client",
            client_secret="secret",
            callback_url="https://review.example/github/oauth/callback",
        )
        use_cases = GitHubInstallationUseCases(mock.Mock(), auth, oauth, "evoagent")

        for installation_id in (
            True,
            77,
            77.9,
            " 77",
            "+77",
            "077",
            "７７",
            "0",
            "999999999999999999999",
        ):
            with self.subTest(installation_id=installation_id):
                with self.assertRaisesRegex(ClientInputError, "installation id"):
                    use_cases.authorize("state", installation_id)

        auth.authenticate_state.assert_not_called()

    def test_signed_oauth_flow_binds_only_the_verified_installation(self):
        user = {
            "id": "user-1",
            "username": "alice",
            "active": True,
            "memberships": [{"tenant_id": "tenant-a", "role": "admin"}],
        }
        store = mock.Mock()
        store.get_user.return_value = user
        consumed_states = set()

        def consume_state(jti, _purpose, _expires_at):
            if jti in consumed_states:
                return False
            consumed_states.add(jti)
            return True

        store.consume_auth_state.side_effect = consume_state
        oauth = mock.Mock(
            client_id="client",
            client_secret="secret",
            callback_url="https://review.example/github/oauth/callback",
        )
        oauth.installation_url.return_value = "https://github.com/install"
        oauth.authorization_url.return_value = "https://github.com/authorize"
        oauth.verify_installation.return_value = "verified-org"
        old_secret = "s" * 32
        auth = AuthManager(store, old_secret)
        use_cases = GitHubInstallationUseCases(store, auth, oauth, "evoagent")
        principal = Principal("user-1", "alice", "tenant-a", "admin")

        self.assertEqual("https://github.com/install", use_cases.begin(principal))
        install_state = oauth.installation_url.call_args.args[1]
        self.assertEqual(
            "https://github.com/authorize",
            use_cases.authorize(install_state, "77"),
        )
        with self.assertRaisesRegex(AccessDeniedError, "already been used"):
            use_cases.authorize(install_state, "77")
        oauth_state, verifier = oauth.authorization_url.call_args.args
        self.assertEqual(43, len(verifier))

        rotated = GitHubInstallationUseCases(
            store,
            AuthManager(store, "n" * 32, previous_secret=old_secret),
            oauth,
            "evoagent",
        )
        for code in ("", "bad code", "x" * 257, True):
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(ClientInputError, "authorization code"),
            ):
                rotated.complete(oauth_state, code)
        result = rotated.complete(oauth_state, "oauth-code")

        self.assertEqual({"installation_id": 77, "account": "verified-org"}, result)
        oauth.verify_installation.assert_called_once_with("oauth-code", verifier, 77)
        store.bind_installation.assert_called_once_with(77, "verified-org", "tenant-a", "alice")
        with self.assertRaisesRegex(AccessDeniedError, "already been used"):
            rotated.complete(oauth_state, "oauth-code")
        with self.assertRaisesRegex(AccessDeniedError, "purpose"):
            rotated.complete(install_state, "oauth-code")

    def test_webhook_requires_its_bound_tenant(self):
        store = mock.Mock()
        reviews = mock.Mock()
        webhooks = WebhookUseCases(
            store,
            reviews,
            lambda: mock.Mock(),
            lambda: None,
            WebhookOptions("default", True),
        )
        payload = {"action": "opened", "installation": {"id": 77}}
        store.get_webhook.return_value = None

        store.installation_tenant.return_value = None
        with self.assertRaisesRegex(AccessDeniedError, "not bound"):
            webhooks.handle_github_pull_request(payload, "delivery-1", "hash-1")

        store.installation_tenant.return_value = "tenant-a"
        with self.assertRaisesRegex(AccessDeniedError, "another tenant"):
            webhooks.handle_github_pull_request(
                payload, "delivery-2", "hash-2", tenant_id="tenant-b"
            )
        reviews.authorize_repository.assert_not_called()

    def test_github_app_webhook_cannot_fall_back_to_the_default_tenant(self):
        store = mock.Mock()
        store.get_webhook.return_value = None
        reviews = mock.Mock()
        webhooks = WebhookUseCases(
            store,
            reviews,
            lambda: mock.Mock(),
            lambda: None,
            WebhookOptions("default", True, require_installation_binding=True),
        )

        with self.assertRaisesRegex(AccessDeniedError, "bound installation"):
            webhooks.handle_github_pull_request({"action": "opened"}, "delivery-1", "hash-1")

        store.claim_webhook.assert_not_called()
        reviews.authorize_repository.assert_not_called()

    def test_webhook_rejects_malformed_payload_shapes_before_admission(self):
        store = mock.Mock()
        store.get_webhook.return_value = None
        reviews = mock.Mock()
        reviews.validate_pull_request.side_effect = ReviewUseCases.validate_pull_request
        webhooks = WebhookUseCases(
            store,
            reviews,
            lambda: mock.Mock(),
            lambda: None,
            WebhookOptions("default", True),
        )
        valid_pull = {
            "diff_url": "https://github.com/org/repo/pull/1.diff",
            "issue_url": "https://api.github.com/repos/org/repo/issues/1",
            "head": {"sha": "abc"},
            "updated_at": "2026-08-23T12:00:00Z",
        }
        malformed = (
            {"action": []},
            {"action": "opened", "installation": []},
            {"action": "opened", "pull_request": []},
            {
                "action": "opened",
                "number": True,
                "repository": {"full_name": "org/repo"},
                "pull_request": valid_pull,
            },
            {
                "action": "opened",
                "number": 1,
                "repository": {"full_name": "org/repo"},
                "pull_request": {
                    **valid_pull,
                    "diff_url": "https://github.com/org/other/pull/1.diff",
                },
            },
            {"action": "opened", "pull_request": {**valid_pull, "draft": "false"}},
        )

        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(ClientInputError):
                webhooks.handle_github_pull_request(payload, "delivery", "hash")

        reviews.authorize_repository.assert_not_called()
        store.accept_pull_request_webhook.assert_not_called()

    def test_webhook_envelope_identity_types_fail_before_store_access(self):
        store = mock.Mock()
        webhooks = WebhookUseCases(
            store,
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            WebhookOptions("default", True),
        )
        valid = {"action": "opened"}
        for payload, delivery_id, digest, tenant_id, message in (
            ([], "delivery", "hash", "", "payload"),
            (valid, "", "hash", "", "delivery id"),
            (valid, True, "hash", "", "delivery id"),
            (valid, "x" * 201, "hash", "", "delivery id"),
            (valid, "delivery", "", "", "digest"),
            (valid, "delivery", True, "", "digest"),
            (valid, "delivery", "hash", [], "tenant"),
            (valid, "delivery", "hash", "x" * 201, "tenant"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ClientInputError, message):
                    webhooks.handle_github_pull_request(payload, delivery_id, digest, tenant_id)

        store.get_webhook.assert_not_called()

    def test_draft_webhooks_are_idempotently_recorded_without_review_work(self):
        store = mock.Mock()
        existing = {"payload_sha256": "hash", "task_id": None}
        store.get_webhook.side_effect = [None, existing, existing]
        store.finish_pull_request_webhook.side_effect = [
            {"accepted": True, "cancelled": 1, "cancel_requested": 1, "released": 1},
            {"accepted": False, "cancelled": 0, "cancel_requested": 0, "released": 0},
        ]
        reviews = mock.Mock()
        reviews.validate_pull_request.side_effect = ReviewUseCases.validate_pull_request
        webhooks = WebhookUseCases(
            store,
            reviews,
            lambda: mock.Mock(),
            lambda: None,
            WebhookOptions("default", True),
        )
        payload = {
            "action": "synchronize",
            "number": 7,
            "repository": {"full_name": "Org/Repo"},
            "pull_request": {"draft": True, "updated_at": "2026-08-23T12:00:00Z"},
        }

        first = webhooks.handle_github_pull_request(payload, "delivery", "hash")
        duplicate = webhooks.handle_github_pull_request(payload, "delivery", "hash")

        self.assertEqual("draft pull request", first["reason"])
        self.assertEqual(1, first["cancelled_tasks"])
        self.assertEqual(1, first["cancellation_requested"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(2, store.finish_pull_request_webhook.call_count)
        self.assertEqual(
            {"org/repo"},
            {call.args[3] for call in store.finish_pull_request_webhook.call_args_list},
        )
        reviews.authorize_repository.assert_not_called()
        store.accept_pull_request_webhook.assert_not_called()

    def test_closed_webhook_ends_the_session_without_new_review_work(self):
        store = mock.Mock()
        store.get_webhook.return_value = None
        store.finish_pull_request_webhook.return_value = {
            "accepted": True,
            "cancelled": 2,
            "cancel_requested": 1,
            "released": 2,
        }
        reviews = mock.Mock()
        reviews.validate_pull_request.side_effect = ReviewUseCases.validate_pull_request
        webhooks = WebhookUseCases(
            store,
            reviews,
            lambda: mock.Mock(),
            lambda: None,
            WebhookOptions("default", True),
        )

        result = webhooks.handle_github_pull_request(
            {
                "action": "closed",
                "number": 7,
                "repository": {"full_name": "org/repo"},
                "pull_request": {
                    "draft": False,
                    "updated_at": "2026-08-23T12:00:00Z",
                },
            },
            "delivery",
            "hash",
        )

        self.assertEqual("closed pull request", result["reason"])
        store.finish_pull_request_webhook.assert_called_once_with(
            "delivery", "default", "hash", "org/repo", 7, "closed", mock.ANY
        )
        reviews.authorize_repository.assert_not_called()
        store.accept_pull_request_webhook.assert_not_called()


class SessionInputBoundaryTests(unittest.TestCase):
    def test_missing_session_is_not_a_malformed_request(self):
        sessions = SessionUseCases(mock.Mock(get_session_timeline=mock.Mock(return_value=None)), 10)

        with self.assertRaisesRegex(ResourceNotFoundError, "session not found"):
            sessions.provide_input("session-1", "production", "tenant-a")

    def test_closed_session_cannot_be_reopened_as_human_input(self):
        store = mock.Mock()
        store.get_session_timeline.return_value = {
            "id": "session-1",
            "tenant_id": "tenant-a",
            "status": "closed",
        }
        sessions = SessionUseCases(store, 10_000)

        with self.assertRaisesRegex(StateConflictError, "not waiting for input"):
            sessions.provide_input("session-1", "production", "tenant-a")

        store.resolve_session_input.assert_not_called()
        store.get_session_timeline.return_value["status"] = "input-required"
        store.resolve_session_input.return_value = False
        with self.assertRaisesRegex(StateConflictError, "not waiting for input"):
            sessions.provide_input("session-1", "production", "tenant-a")
        store.resolve_session_input.assert_called_once_with("session-1", "tenant-a", "system")
        store.audit.assert_not_called()

    def test_input_delegates_actor_and_secret_free_audit_to_store_transaction(self):
        store = mock.Mock()
        store.get_session_timeline.return_value = {
            "id": "session-1",
            "tenant_id": "tenant-a",
            "status": "input-required",
        }
        store.resolve_session_input.return_value = True
        sessions = SessionUseCases(store, 10_000)

        result = sessions.provide_input("session-1", "password=do-not-audit", "tenant-a", "alice")

        self.assertEqual({"session_id": "session-1", "status": "open"}, result)
        store.resolve_session_input.assert_called_once_with("session-1", "tenant-a", "alice")
        store.audit.assert_not_called()


class FeedbackBoundaryTests(unittest.TestCase):
    def test_feedback_delegates_tenant_actor_and_audit_to_store_transaction(self):
        store = mock.Mock()
        store.record_failure_case.return_value = True
        use_cases = ReviewUseCases.__new__(ReviewUseCases)
        use_cases.store = store

        result = use_cases.record_feedback(
            "task-1",
            "false_positive",
            {"rule_id": "SEC-EVAL"},
            "password=learning-sample",
            "tenant-a",
            "alice",
        )

        self.assertEqual({"recorded": True, "category": "false_positive"}, result)
        store.get.assert_not_called()
        store.record_failure_case.assert_called_once_with(
            "task-1",
            "false_positive",
            {
                "finding": {"rule_id": "SEC-EVAL"},
                "note": "password=learning-sample",
            },
            tenant_id="tenant-a",
            actor="alice",
        )

    def test_feedback_rejects_wrong_tenant_without_counting_it(self):
        store = mock.Mock()
        store.record_failure_case.return_value = False
        use_cases = ReviewUseCases.__new__(ReviewUseCases)
        use_cases.store = store

        with self.assertRaisesRegex(ResourceNotFoundError, "task not found"):
            use_cases.record_feedback("task-1", "accepted", None, "looks good", "tenant-b", "bob")


class ReviewInputBoundaryTests(unittest.TestCase):
    def test_pull_request_matches_the_persisted_integer_domain(self):
        for value in (True, 0, -1, 2**31, "7"):
            with self.subTest(value=value), self.assertRaises(ClientInputError):
                ReviewUseCases.validate_pull_request(value)  # type: ignore[arg-type]

    def test_diff_must_be_valid_utf8(self):
        reviews = mock.Mock(options=ReviewOptions(10_000, 60, False))

        with self.assertRaisesRegex(ClientInputError, "valid UTF-8"):
            ReviewUseCases.validate(reviews, "org/repo", "\ud800")


class RolloutAssignmentSnapshotTests(unittest.TestCase):
    def test_shadow_failure_does_not_change_a_successful_primary_review(self):
        report = ReviewReport("org/repo", 7, "done", "low", [])
        store = mock.Mock()
        store.create_review_task.return_value = True
        policies = mock.Mock()
        policies.resolve.return_value = RepositoryPolicy()
        policies.snapshot.return_value = {"version": 0, "policy": {}}
        releases = mock.Mock()
        releases.assignment.return_value = {
            "lane": "stable",
            "shadow": False,
            "deployment": None,
        }
        shadow = mock.Mock(side_effect=RuntimeError("shadow unavailable"))
        store.audit.side_effect = RuntimeError("audit unavailable")
        reviews = ReviewUseCases(
            store,
            policies,
            releases,
            mock.Mock(),
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("reviewer", "local", ""),
            mock.Mock(return_value=report),
            shadow,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, False),
            reviewer_revision=lambda: "reviewer-revision",
        )
        reviews._review_succeeded = mock.Mock()
        reviews._review_failed = mock.Mock()

        captured = Metrics()
        with mock.patch("evoagent.application.reviews.metrics", captured):
            result = reviews.create_review("org/repo", "diff", 7, tenant_id="tenant-a")

        self.assertEqual("SUCCESS", result["state"])
        reviews._review_succeeded.assert_called_once()
        reviews._review_failed.assert_not_called()
        self.assertEqual("shadow.failed", store.audit.call_args.args[2])
        self.assertIn("evoagent_shadow_audit_failures_total 1.0", captured.prometheus())

    def test_review_task_persists_the_assigned_release_versions(self):
        store = mock.Mock()
        policies = mock.Mock()
        policies.snapshot.return_value = {"version": 0, "policy": {}}
        releases = mock.Mock()
        releases.assignment.return_value = {
            "lane": "canary",
            "shadow": True,
            "deployment": {"stable_version": 1, "candidate_version": 2, "generation": 7},
        }
        reviews = ReviewUseCases(
            store,
            policies,
            releases,
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("reviewer", "provider", "model"),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, False),
            reviewer_revision=lambda: "reviewer-revision",
        )

        reviews.create_task("Org/Repo", "diff", 7, "api", "tenant-a", actor="alice")

        self.assertEqual("org/repo", store.create_review_task.call_args.args[1])
        self.assertEqual("alice", store.create_review_task.call_args.args[-1])
        task_input = store.create_review_task.call_args.args[3]
        self.assertEqual("canary", task_input["release_lane"])
        self.assertTrue(task_input["shadow"])
        self.assertEqual(1, task_input["release_stable_version"])
        self.assertEqual(2, task_input["release_candidate_version"])
        self.assertEqual(7, task_input["release_generation"])
        self.assertEqual("reviewer-revision", task_input["reviewer_revision"])


class RepairCredentialBindingTests(unittest.TestCase):
    def test_fix_receipt_status_drives_replay_and_conflict(self):
        raw = {"branch": "evoagent/fix", "commits": [{"sha": "abc"}]}
        store = mock.Mock()
        store.get.return_value = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "input": {"head_sha": "reviewed-sha"},
            "report": {"findings": []},
        }
        store.claim_effect.side_effect = (
            {"status": "acquired"},
            {"status": "completed", "result": raw},
            {"status": "busy"},
        )
        store.complete_effect.return_value = True
        policies = mock.Mock()
        policies.authorize_fix.return_value = ()
        fixer = mock.Mock(rule_ids=())
        fixer.create_fix_commits.return_value = raw
        repairs = RepairUseCases.__new__(RepairUseCases)
        repairs.store = store
        repairs.policies = policies
        repairs.fixer = fixer
        repairs.code_host_for_installation = mock.Mock(return_value=object())
        repairs.options = mock.Mock(effect_lease_seconds=30)
        captured = Metrics()

        with mock.patch("evoagent.application.repairs.metrics", captured):
            created = repairs.create_fix("task", "tenant")
            replayed = repairs.create_fix("task", "tenant")
            with self.assertRaisesRegex(StateConflictError, "already in progress"):
                repairs.create_fix("task", "tenant")

        self.assertEqual({**raw, "replayed": False}, created)
        self.assertEqual({**raw, "replayed": True}, replayed)
        fixer.create_fix_commits.assert_called_once()
        audit_event = store.complete_effect.call_args.kwargs["audit_event"]
        self.assertEqual(
            (
                "tenant",
                "system",
                "repair.create",
                "task",
                {"branch": raw["branch"], "replayed": False},
            ),
            audit_event,
        )
        store.audit.assert_called_once_with(
            "tenant",
            "system",
            "repair.create",
            "task",
            {"branch": raw["branch"], "replayed": True},
        )
        self.assertIn("evoagent_fix_idempotent_replays_total 1.0", captured.prometheus())

    def test_fix_distinguishes_missing_from_incomplete_task(self):
        store = mock.Mock()
        repairs = RepairUseCases.__new__(RepairUseCases)
        repairs.store = store

        store.get.return_value = None
        with self.assertRaisesRegex(ResourceNotFoundError, "completed task not found"):
            repairs.create_fix("task", "tenant")

        store.get.return_value = {"report": None}
        with self.assertRaisesRegex(StateConflictError, "review is not complete"):
            repairs.create_fix("task", "tenant")

    def test_fix_policy_is_rechecked_after_verification_before_publication(self):
        store = mock.Mock()
        store.get.return_value = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "input": {"head_sha": "reviewed-sha"},
            "report": {"findings": []},
        }
        store.claim_effect.return_value = {"status": "acquired"}
        policies = mock.Mock()
        policies.resolve.side_effect = (
            RepositoryPolicy(auto_fix=True),
            RepositoryPolicy(auto_fix=False),
        )
        policies.authorize_fix.side_effect = RepositoryPolicyResolver.authorize_fix
        fixer = mock.Mock(rule_ids=("SEC-HARDCODED-SECRET",))

        def publish(*_args, **kwargs):
            kwargs["before_publish"](("SEC-HARDCODED-SECRET",))

        fixer.create_fix_commits.side_effect = publish
        repairs = RepairUseCases(
            store,
            policies,
            fixer,
            mock.Mock(return_value=object()),
            RepairOptions(10, 1, "", 1, 1, 1.0, 1, 1.0),
        )

        with self.assertRaisesRegex(AccessDeniedError, "not enabled"):
            repairs.create_fix("task", "tenant")

        self.assertEqual(2, policies.resolve.call_count)
        store.complete_effect.assert_not_called()
        store.release_effect.assert_called_once()

    def test_fix_uses_task_snapshotted_installation(self):
        store = mock.Mock()
        store.get.return_value = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "input": {"installation_id": 77, "head_sha": "reviewed-sha"},
            "report": {"findings": []},
        }
        store.installation_tenant.return_value = "tenant"
        store.claim_effect.return_value = {"status": "acquired"}
        store.complete_effect.return_value = True
        policies = mock.Mock()
        policies.authorize_fix.return_value = ()
        fixer = mock.Mock(rule_ids=())
        fixer.create_fix_commits.return_value = {"branch": None, "commits": []}
        code_host = mock.Mock(return_value=object())
        repairs = RepairUseCases(
            store,
            policies,
            fixer,
            code_host,
            RepairOptions(10, 1, "", 1, 1, 1.0, 1, 1.0),
        )

        repairs.create_fix("task", "tenant")

        code_host.assert_called_once_with(77)
        self.assertEqual(
            "reviewed-sha",
            fixer.create_fix_commits.call_args.kwargs["expected_source_sha"],
        )

    def test_fix_renews_effect_ownership_before_each_provider_write(self):
        store = mock.Mock()
        store.get.return_value = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "input": {"head_sha": "reviewed-sha"},
            "report": {"findings": []},
        }
        store.claim_effect.return_value = {"status": "acquired"}
        store.renew_effect.side_effect = (True, False)
        policies = mock.Mock()
        policies.authorize_fix.return_value = ()
        fixer = mock.Mock(rule_ids=())

        def publish(*_args, **kwargs):
            kwargs["before_publish"](())
            kwargs["before_publish"](())

        fixer.create_fix_commits.side_effect = publish
        repairs = RepairUseCases(
            store,
            policies,
            fixer,
            mock.Mock(return_value=object()),
            RepairOptions(10, 1, "", 1, 1, 1.0, 1, 300),
        )

        captured = Metrics()
        with (
            mock.patch("evoagent.application.repairs.metrics", captured),
            self.assertRaisesRegex(RuntimeError, "lease was lost before publication"),
        ):
            repairs.create_fix("task", "tenant")

        self.assertEqual(2, store.renew_effect.call_count)
        store.complete_effect.assert_not_called()
        store.release_effect.assert_called_once()
        self.assertIn("evoagent_effect_lease_conflicts_total 1.0", captured.prometheus())

    def test_fix_without_a_snapshotted_head_is_rejected(self):
        store = mock.Mock()
        store.get.return_value = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "input": {},
            "report": {"findings": []},
        }
        fixer = mock.Mock(rule_ids=())
        repairs = RepairUseCases(
            store,
            mock.Mock(),
            fixer,
            mock.Mock(),
            RepairOptions(10, 1, "", 1, 1, 1.0, 1, 1.0),
        )

        with self.assertRaisesRegex(StateConflictError, "snapshotted PR head"):
            repairs.create_fix("task", "tenant")

        fixer.create_fix_commits.assert_not_called()
        store.claim_effect.assert_not_called()

    def test_fix_rejects_an_installation_bound_to_another_tenant(self):
        store = mock.Mock()
        store.get.return_value = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant-a",
            "input": {"installation_id": 77, "head_sha": "reviewed-sha"},
            "report": {"findings": []},
        }
        store.installation_tenant.return_value = "tenant-b"
        fixer = mock.Mock(rule_ids=())
        repairs = RepairUseCases(
            store,
            mock.Mock(),
            fixer,
            mock.Mock(),
            RepairOptions(10, 1, "", 1, 1, 1.0, 1, 1.0),
        )

        with self.assertRaisesRegex(AccessDeniedError, "not bound to task tenant"):
            repairs.create_fix("task", "tenant-a")

        fixer.create_fix_commits.assert_not_called()
        store.claim_effect.assert_not_called()


class StructuredEvidenceInputTests(unittest.TestCase):
    def test_impact_rejects_invalid_entries_instead_of_silently_dropping_them(self):
        sessions = SessionUseCases(mock.Mock(), max_diff_bytes=10_000)

        with self.assertRaisesRegex(ClientInputError, "string paths to string contents"):
            sessions.analyze_impact({"app.py": "x = 1\n", "bad.py": 1}, ["app.py"])
        with self.assertRaisesRegex(ClientInputError, "only string paths"):
            sessions.analyze_impact({"app.py": "x = 1\n"}, ["app.py", 1])

    def test_impact_budget_includes_paths_and_changed_list(self):
        sessions = SessionUseCases(mock.Mock(), max_diff_bytes=1)

        with self.assertRaisesRegex(ClientInputError, "maximum analysable size"):
            sessions.analyze_impact({"long-name.py": ""}, [])
        with self.assertRaisesRegex(ClientInputError, "too many files or changed paths"):
            sessions.analyze_impact({}, ["a.py"] * 5001)

    def test_proof_rejects_invalid_entries_before_running_evidence(self):
        executor = mock.Mock()
        repairs = RepairUseCases(
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            RepairOptions(10_000, 1, "", 1, 1, 1.0, 1, 300),
            executor,
        )

        for original, patched in (
            ({"app.py": 1}, {"app.py": "fixed\n"}),
            ({"app.py": "bug\n"}, {1: "fixed\n"}),
        ):
            with (
                self.subTest(original=original, patched=patched),
                self.assertRaisesRegex(ClientInputError, "string paths to string contents"),
            ):
                repairs.run_proof(original, patched, "pytest")

        executor.execute.assert_not_called()

    def test_proof_budget_includes_paths_and_rejects_invalid_utf8(self):
        executor = mock.Mock()
        repairs = RepairUseCases(
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            RepairOptions(1, 1, "", 1, 1, 1.0, 1, 300),
            executor,
        )

        with self.assertRaisesRegex(ClientInputError, "maximum analysable size"):
            repairs.run_proof({"long-name.py": ""}, {})
        with self.assertRaisesRegex(ClientInputError, "valid UTF-8"):
            repairs.run_proof({"\ud800": ""}, {})

        executor.execute.assert_not_called()


class QueueTaskBindingTests(unittest.TestCase):
    def test_cancel_response_reports_the_durable_task_state(self):
        reviews = ReviewUseCases.__new__(ReviewUseCases)
        reviews.store = mock.Mock()
        reviews.store.request_cancel.return_value = True
        reviews.store.get.return_value = {"state": "SUCCESS", "cancel_requested": False}

        self.assertEqual(
            {"accepted": True, "cancel_requested": False, "state": "SUCCESS"},
            reviews.cancel_task("task-1", "tenant-a", "alice"),
        )
        reviews.store.request_cancel.assert_called_once_with("task-1", "tenant-a", "alice")
        reviews.store.get.assert_called_once_with("task-1", "tenant-a")

    def test_idempotent_replay_returns_the_durable_task_state(self):
        reviews = ReviewUseCases.__new__(ReviewUseCases)
        reviews.authorize_review = mock.Mock(return_value=RepositoryPolicy())
        reviews.create_task = mock.Mock(return_value=("task-1", False))
        reviews.store = mock.Mock()
        reviews.store.get.return_value = {"state": "SUCCESS"}
        reviews.notify_outbox = mock.Mock()
        reviews.queue = mock.Mock(return_value=mock.Mock(backend="redis-streams"))

        result = reviews.enqueue_review(
            "org/repo",
            "diff",
            7,
            tenant_id="tenant-a",
            idempotency_key="retry-1",
        )

        self.assertEqual(
            {
                "task_id": "task-1",
                "state": "SUCCESS",
                "queue": "redis-streams",
                "replayed": True,
            },
            result,
        )
        reviews.store.get.assert_called_once_with("task-1", "tenant-a")

        reviews.create_task.return_value = ("task-2", True)
        created = reviews.enqueue_review("org/repo", "diff", 7, tenant_id="tenant-a")
        self.assertEqual(
            {
                "task_id": "task-2",
                "state": "PENDING",
                "queue": "redis-streams",
                "replayed": False,
            },
            created,
        )
        reviews.store.get.assert_called_once()

    def test_synchronous_exception_accounting_follows_durable_state(self):
        reviews = ReviewUseCases.__new__(ReviewUseCases)
        reviews.store = mock.Mock()
        reviews.store.get.side_effect = (
            {"state": "FAILED"},
            {"state": "CANCELLED"},
            {"state": "PLANNING"},
            RuntimeError("task store unavailable"),
        )
        reviews._review_failed = mock.Mock()
        captured = Metrics()

        with mock.patch("evoagent.application.reviews.metrics", captured):
            for _ in range(4):
                reviews._account_synchronous_exception("task-1", "tenant-a")

        reviews._review_failed.assert_called_once_with("task-1", "tenant-a", "synchronous")
        output = captured.prometheus()
        self.assertIn("evoagent_reviews_cancelled_total 1.0", output)
        self.assertIn("evoagent_review_admission_releases_total 1.0", output)
        self.assertIn("evoagent_review_terminal_accounting_failures_total 2.0", output)

    def test_success_is_not_reversed_by_governance_observation_failures(self):
        reviews = ReviewUseCases.__new__(ReviewUseCases)
        reviews.store = mock.Mock()
        reviews.store.get.return_value = {"input": {}}
        reviews.releases = mock.Mock()
        reviews.releases.observe.side_effect = RuntimeError("rollout store unavailable")
        reviews.alerts = mock.Mock()
        reviews.alerts.evaluate.side_effect = RuntimeError("alert store unavailable")
        captured = Metrics()

        with mock.patch("evoagent.application.reviews.metrics", captured):
            reviews._review_succeeded("task-1", "tenant-a")

        reviews.alerts.evaluate.assert_called_once_with("tenant-a")
        reviews.releases.observe.assert_called_once_with(
            "tenant-a", "llm-review", "task-1", False, "stable", None, None
        )
        output = captured.prometheus()
        self.assertIn("evoagent_release_observation_failures_total 1.0", output)
        self.assertIn("evoagent_alert_evaluation_failures_total 1.0", output)

        reviews.store.get.side_effect = RuntimeError("task store unavailable")
        with mock.patch("evoagent.application.reviews.metrics", captured):
            reviews._review_succeeded("task-1", "tenant-a")

        self.assertEqual(2, reviews.alerts.evaluate.call_count)
        self.assertEqual(1, reviews.releases.observe.call_count)
        output = captured.prometheus()
        self.assertIn("evoagent_release_observation_failures_total 2.0", output)
        self.assertIn("evoagent_alert_evaluation_failures_total 2.0", output)

    def test_missing_policy_snapshot_is_a_permanent_delivery_failure(self):
        store = mock.Mock()
        store.get.return_value = {
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {},
        }
        store.review_admission_active.return_value = True
        policies = mock.Mock()
        policies.from_snapshot.side_effect = RepositoryPolicyResolver.from_snapshot
        execute = mock.Mock()
        reviews = ReviewUseCases(
            store,
            policies,
            mock.Mock(),
            mock.Mock(),
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        with self.assertRaisesRegex(PermanentTaskError, "policy snapshot"):
            reviews.process_queued(
                {
                    "task_id": "task-1",
                    "tenant_id": "tenant-a",
                    "repository": "org/repo",
                    "pull_request": 7,
                    "admission_generation": 1,
                }
            )

        policies.resolve.assert_not_called()
        store.get_task_payload.assert_not_called()
        execute.assert_not_called()

    def test_corrupt_queue_identity_cannot_change_task_tenant_or_repository(self):
        store = mock.Mock()
        store.get.return_value = {
            "id": "task-1",
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {},
        }
        policies = mock.Mock()
        execute = mock.Mock()
        reviews = ReviewUseCases(
            store,
            policies,
            mock.Mock(),
            mock.Mock(),
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        for changed in (
            {"tenant_id": "tenant-b", "repository": "org/repo"},
            {"tenant_id": "tenant-a", "repository": "other/repo"},
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(PermanentTaskError, "binding"),
            ):
                reviews.process_queued({"task_id": "task-1", "pull_request": 7, **changed})

        policies.resolve.assert_not_called()
        execute.assert_not_called()

    def test_stale_admission_generation_cannot_execute_a_review(self):
        store = mock.Mock()
        store.get.return_value = {
            "state": "FAILED",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
        }
        store.review_admission_active.side_effect = (False, True)
        execute = mock.Mock()
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )
        payload = {
            "task_id": "task-1",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "admission_generation": 1,
        }

        captured = Metrics()
        with mock.patch("evoagent.application.reviews.metrics", captured):
            reviews.process_queued(payload)

        self.assertEqual(
            [mock.call("task-1", 1), mock.call("task-1")],
            store.review_admission_active.call_args_list,
        )
        self.assertIn("evoagent_queue_terminal_duplicates_total 1.0", captured.prometheus())
        execute.assert_not_called()
        store.get_task_payload.assert_not_called()
        store.review_admission_active.reset_mock()
        store.review_admission_active.side_effect = (False, False)
        with self.assertRaisesRegex(PermanentTaskError, "inactive or stale"):
            reviews.process_queued(payload)

        self.assertEqual(
            [mock.call("task-1", 1), mock.call("task-1")],
            store.review_admission_active.call_args_list,
        )
        execute.assert_not_called()
        store.get_task_payload.assert_not_called()

    def test_cancelled_execution_is_acked_without_failure_observation(self):
        task = {
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {},
        }
        store = mock.Mock()
        store.get.return_value = task
        store.review_admission_active.return_value = True
        store.get_task_payload.return_value = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        policy = RepositoryPolicy(enabled=True)
        policies = mock.Mock()
        policies.resolve.return_value = policy
        releases = mock.Mock()

        def cancel_during_execution(*_args):
            task["state"] = "CANCELLED"
            raise RuntimeError("cancelled")

        reviews = ReviewUseCases(
            store,
            policies,
            releases,
            mock.Mock(),
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            cancel_during_execution,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        reviews.process_queued(
            {
                "task_id": "task-1",
                "tenant_id": "tenant-a",
                "repository": "org/repo",
                "pull_request": 7,
                "admission_generation": 1,
            }
        )

        releases.observe.assert_not_called()
        store.get_task_payload.assert_called_once_with("task-1")

    def test_losing_worker_delivers_winning_report_without_failure_observation(self):
        report = ReviewReport("org/repo", 7, "winner", "low", [])
        task = {
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {},
        }
        store = mock.Mock()
        store.get.return_value = task
        store.review_admission_active.return_value = True
        store.get_task_payload.return_value = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        policy = RepositoryPolicy(enabled=True)
        policies = mock.Mock()
        policies.resolve.return_value = policy
        releases = mock.Mock()
        alerts = mock.Mock()

        def lose_race(*_args):
            task.update({"state": "SUCCESS", "report": report.to_dict()})
            raise RuntimeError("another worker won")

        reviews = ReviewUseCases(
            store,
            policies,
            releases,
            alerts,
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            lose_race,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )
        reviews._deliver_review_result = mock.Mock()
        captured = Metrics()

        with mock.patch("evoagent.application.reviews.metrics", captured):
            reviews.process_queued(
                {
                    "task_id": "task-1",
                    "tenant_id": "tenant-a",
                    "repository": "org/repo",
                    "pull_request": 7,
                    "admission_generation": 1,
                }
            )

        delivered = reviews._deliver_review_result.call_args.args[3]
        self.assertEqual("winner", delivered.summary)
        releases.observe.assert_not_called()
        alerts.evaluate.assert_not_called()
        output = captured.prometheus()
        self.assertIn("evoagent_queue_terminal_duplicates_total 1.0", output)
        self.assertNotIn("evoagent_reviews_failed_total", output)

    def test_only_dead_letter_counts_an_async_terminal_failure(self):
        task = {
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {
                "release_lane": "canary",
                "release_candidate_version": 2,
                "release_generation": 3,
            },
        }
        store = mock.Mock()
        store.get.return_value = task
        store.review_admission_active.return_value = True
        store.get_task_payload.return_value = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        store.release_review_admission.side_effect = (True, False)
        policy = RepositoryPolicy(enabled=True)
        policies = mock.Mock()
        policies.from_snapshot.return_value = policy
        policies.resolve.return_value = policy
        releases = mock.Mock()
        alerts = mock.Mock()
        reviews = ReviewUseCases(
            store,
            policies,
            releases,
            alerts,
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            mock.Mock(side_effect=RuntimeError("transient provider failure")),
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )
        payload = {
            "task_id": "task-1",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "admission_generation": 1,
        }
        captured = Metrics()

        with (
            mock.patch("evoagent.application.reviews.metrics", captured),
            self.assertRaisesRegex(RuntimeError, "transient provider failure"),
        ):
            reviews.process_queued(payload)

        output = captured.prometheus()
        self.assertIn("evoagent_review_attempts_failed_total 1.0", output)
        self.assertNotIn("evoagent_reviews_failed_total", output)
        releases.observe.assert_not_called()
        alerts.evaluate.assert_not_called()

        task["state"] = "FAILED"
        with mock.patch("evoagent.application.reviews.metrics", captured):
            reviews.on_dead_letter(payload, "task delivery failed")
            reviews.on_dead_letter(payload, "duplicate dead letter")

        self.assertIn("evoagent_reviews_failed_total 1.0", captured.prometheus())
        releases.observe.assert_called_once_with(
            "tenant-a", "llm-review", "task-1", True, "canary", 2, 3
        )
        alerts.evaluate.assert_called_once_with("tenant-a")

    def test_policy_kill_switch_is_rechecked_after_review_before_commenting(self):
        report = ReviewReport("org/repo", 7, "done", "low", [])
        task = {
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {
                "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
                "repository_policy": {"version": 1, "policy": {}},
            },
        }
        store = mock.Mock()
        store.get.return_value = task
        store.review_admission_active.return_value = True
        store.get_task_payload.return_value = "patch"
        policies = mock.Mock()
        snapshot_policy = RepositoryPolicy(enabled=True, post_review_comments=True)
        policies.from_snapshot.return_value = snapshot_policy
        policies.resolve.side_effect = (
            RepositoryPolicy(enabled=True, post_review_comments=True),
            RepositoryPolicy(enabled=False, post_review_comments=False),
        )
        execute = mock.Mock(return_value=report)
        code_host = mock.Mock()
        reviews = ReviewUseCases(
            store,
            policies,
            mock.Mock(),
            mock.Mock(),
            mock.MagicMock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "",
            code_host,
            ReviewOptions(10_000, 60, True),
        )

        reviews.process_queued(
            {
                "task_id": "task-1",
                "tenant_id": "tenant-a",
                "repository": "org/repo",
                "pull_request": 7,
                "admission_generation": 1,
            }
        )

        self.assertEqual(2, policies.resolve.call_count)
        execute.assert_called_once()
        self.assertEqual(1, execute.call_args.args[-1])
        code_host.assert_not_called()
        store.claim_effect.assert_not_called()
        store.update_task_input.assert_called_with(
            "task-1", {"_delivery_complete": True, "_delivery_resume_active": False}
        )

    def test_successful_review_retries_only_its_pending_comment_effect(self):
        report = ReviewReport("org/repo", 7, "done", "low", [])
        task = {
            "state": "SUCCESS",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "report": report.to_dict(),
            "input": {
                "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
                "installation_id": 77,
            },
        }
        store = mock.Mock()
        store.get.return_value = task
        store.installation_tenant.return_value = "tenant-b"
        store.claim_effect.return_value = {"status": "acquired"}
        store.complete_effect.return_value = True
        store.update_task_input.side_effect = lambda _task_id, values: task["input"].update(values)
        policy = RepositoryPolicy(enabled=True, post_review_comments=True)
        policies = mock.Mock()
        policies.resolve.return_value = policy
        client = mock.Mock()
        client.upsert_comment.side_effect = [RuntimeError("GitHub unavailable"), None]
        execute = mock.Mock()
        record_session = mock.Mock(return_value="continuity")
        releases = mock.Mock()
        reviews = ReviewUseCases(
            store,
            policies,
            releases,
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            record_session,
            lambda _installation_id: client,
            ReviewOptions(10_000, 60, True),
        )
        payload = {
            "task_id": "task-1",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
            "installation_id": 77,
        }

        with self.assertRaisesRegex(PermanentTaskError, "not bound to persisted tenant"):
            reviews.process_queued(payload)
        client.upsert_comment.assert_not_called()

        store.installation_tenant.return_value = "tenant-a"
        with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
            reviews.process_queued(payload)
        reviews.process_queued(payload)

        execute.assert_not_called()
        record_session.assert_called_once()
        self.assertEqual(2, client.upsert_comment.call_count)
        self.assertEqual(1, len({call.args[0] for call in store.claim_effect.call_args_list}))
        self.assertEqual({60}, {call.args[2] for call in store.claim_effect.call_args_list})
        store.release_effect.assert_called_once()
        store.complete_effect.assert_called_once()
        releases.observe.assert_not_called()
        store.get_task_payload.assert_not_called()
        self.assertTrue(task["input"]["_delivery_complete"])

    def test_ended_session_suppresses_a_pending_github_comment(self):
        report = ReviewReport("org/repo", 7, "done", "low", [])
        task = {
            "state": "SUCCESS",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "report": report.to_dict(),
            "input": {
                "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
                "installation_id": 77,
                "session_id": "session-1",
                "turn_id": "turn-1",
            },
        }
        store = mock.Mock()
        store.get.return_value = task
        store.installation_tenant.return_value = "tenant-a"
        store.get_session_timeline.return_value = {
            "status": "closed",
            "turns": [{"id": "turn-1"}],
        }
        policies = mock.Mock()
        policies.resolve.return_value = RepositoryPolicy(enabled=True, post_review_comments=True)
        client = mock.Mock()
        reviews = ReviewUseCases(
            store,
            policies,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            mock.Mock(),
            lambda *_args: None,
            mock.Mock(return_value=""),
            lambda _installation_id: client,
            ReviewOptions(10_000, 60, True),
        )

        reviews.process_queued(
            {
                "task_id": "task-1",
                "tenant_id": "tenant-a",
                "repository": "org/repo",
                "pull_request": 7,
            }
        )

        client.upsert_comment.assert_not_called()
        store.claim_effect.assert_not_called()
        store.update_task_input.assert_called_with(
            "task-1", {"_delivery_complete": True, "_delivery_resume_active": False}
        )

    def test_each_current_session_turn_updates_the_same_comment_once(self):
        store = mock.Mock()
        store.claim_effect.return_value = {"status": "acquired"}
        store.renew_effect.return_value = True
        store.complete_effect.return_value = True
        store.get_session_timeline.side_effect = (
            {"status": "open", "turns": [{"id": "turn-1"}]},
            {"status": "open", "turns": [{"id": "turn-2"}]},
        )
        client = mock.Mock()

        def publish(*_args, **kwargs):
            kwargs["before_write"]()

        client.upsert_comment.side_effect = publish
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            mock.Mock(),
            lambda *_args: None,
            mock.Mock(return_value=""),
            lambda _installation_id: client,
            ReviewOptions(10_000, 60, True),
        )
        payload = {
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
            "installation_id": 77,
            "session_id": "session-1",
        }
        report = ReviewReport("org/repo", 7, "done", "low", [])

        reviews._post_review_comment({**payload, "turn_id": "turn-1"}, "task-1", report, "")
        reviews._post_review_comment({**payload, "turn_id": "turn-2"}, "task-2", report, "")

        effect_keys = [call.args[0] for call in store.claim_effect.call_args_list]
        self.assertEqual(2, len(set(effect_keys)))
        self.assertEqual(2, client.upsert_comment.call_count)
        self.assertEqual(
            {"<!-- evoagent-session:session-1 -->"},
            {call.args[2] for call in client.upsert_comment.call_args_list},
        )

    def test_comment_kill_switch_is_rechecked_at_provider_write_boundary(self):
        store = mock.Mock()
        store.claim_effect.return_value = {"status": "acquired"}
        store.renew_effect.return_value = True
        store.complete_effect.return_value = True
        policies = mock.Mock()
        policies.resolve.return_value = RepositoryPolicy(enabled=False, post_review_comments=False)
        writes = []
        client = mock.Mock()

        def publish(*_args, **kwargs):
            kwargs["before_write"]()
            writes.append(True)

        client.upsert_comment.side_effect = publish
        reviews = ReviewUseCases(
            store,
            policies,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            mock.Mock(),
            lambda *_args: None,
            mock.Mock(return_value=""),
            lambda _installation_id: client,
            ReviewOptions(10_000, 60, True),
        )

        reviews._post_review_comment(
            {
                "tenant_id": "tenant-a",
                "repository": "org/repo",
                "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
            },
            "task-1",
            ReviewReport("org/repo", 7, "done", "low", []),
            "",
        )

        self.assertEqual([], writes)
        store.complete_effect.assert_called_once_with(
            mock.ANY,
            mock.ANY,
            {"marker": "<!-- evoagent-review:task-1 -->", "suppressed": True},
        )
        store.release_effect.assert_not_called()

    def test_github_comment_omits_whole_findings_to_stay_within_its_budget(self):
        store = mock.Mock()
        store.claim_effect.return_value = {"status": "acquired"}
        store.renew_effect.return_value = True
        store.complete_effect.return_value = True
        client = mock.Mock()

        def publish(*_args, **kwargs):
            kwargs["before_write"]()

        client.upsert_comment.side_effect = publish
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            mock.Mock(),
            lambda *_args: None,
            mock.Mock(return_value=""),
            lambda _installation_id: client,
            ReviewOptions(10_000, 60, True),
        )
        finding = _finding()
        finding.explanation = "oversized-detail-" * 5000
        report = ReviewReport("org/repo", 7, "done", "high", [finding])

        captured = Metrics()
        with mock.patch("evoagent.application.reviews.metrics", captured):
            reviews._post_review_comment(
                {
                    "repository": "org/repo",
                    "github_issue_url": "https://api.github.com/repos/org/repo/issues/7",
                },
                "task-1",
                report,
                "",
            )

        body, marker = client.upsert_comment.call_args.args[1:]
        store.renew_effect.assert_called_once()
        self.assertLessEqual(len(marker + "\n" + body), 60_000)
        self.assertIn("Comment truncated", body)
        self.assertNotIn("oversized-detail", body)
        self.assertIn("evoagent_github_comment_truncations_total 1.0", captured.prometheus())

    def test_only_the_latest_open_session_turn_may_publish(self):
        store = mock.Mock()
        store.get_session_timeline.side_effect = (
            None,
            {"status": "closed", "turns": [{"id": "turn-1"}]},
            {"status": "open", "turns": [{"id": "turn-2"}]},
            {"status": "open", "turns": [{"id": "turn-1"}]},
        )
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            mock.Mock(),
            lambda *_args: None,
            mock.Mock(return_value=""),
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )
        payload = {"session_id": "session-1", "turn_id": "turn-1", "tenant_id": "tenant-a"}

        self.assertEqual(
            [False, False, False, True],
            [reviews._session_publishable(payload) for _ in range(4)],
        )

    def test_dead_letter_uses_persisted_tenant_and_strict_generation(self):
        store = mock.Mock()
        store.get.return_value = {
            "state": "PENDING",
            "tenant_id": "tenant-a",
            "input": {},
        }
        store.release_review_admission.return_value = False
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        reviews.on_dead_letter(
            {"task_id": "task-1", "tenant_id": "tenant-b", "admission_generation": 1},
            "task delivery failed",
        )

        store.create_alert.assert_called_once_with("tenant-a", "dlq:task-1", "critical", mock.ANY)
        self.assertEqual(1, store.fail.call_args.args[-1])
        for raw_generation in (None, True, "1"):
            store.fail.reset_mock()
            store.release_review_admission.reset_mock()
            reviews.on_dead_letter(
                {"task_id": "task-1", "admission_generation": raw_generation},
                "task delivery failed",
            )
            store.fail.assert_not_called()
            store.release_review_admission.assert_called_once_with("task-1", "dead-letter", -1)

    def test_delivery_dead_letter_reopens_operator_resume(self):
        store = mock.Mock()
        store.get.return_value = {
            "state": "SUCCESS",
            "tenant_id": "tenant-a",
            "input": {"_delivery_resume_active": True},
        }
        store.release_review_admission.return_value = False
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        reviews.on_dead_letter(
            {
                "task_id": "task-1",
                "delivery_only": True,
                "_queue_message_id": "review-delivery:1",
            },
            "failed",
        )

        store.release_review_delivery_resume.assert_called_once_with("task-1", "review-delivery:1")

    def test_resume_accepts_snapshotted_deferred_diff(self):
        store = mock.Mock()
        store.get.return_value = {
            "state": "FAILED",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "input": {"diff_url": "https://api.github.com/repos/org/repo/pulls/7.diff"},
        }
        store.get_task_payload.return_value = None
        store.resume_review_task.return_value = {"status": "resumed", "generation": 2}
        notify = mock.Mock()
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            notify,
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        result = reviews.resume_task("task-1", "tenant-a", "alice")

        self.assertTrue(result["resumed"])
        self.assertEqual("alice", store.resume_review_task.call_args.args[-1])
        notify.assert_called_once_with()

    def test_resume_successful_task_republishes_only_incomplete_delivery(self):
        store = mock.Mock()
        store.get.return_value = {
            "state": "SUCCESS",
            "tenant_id": "tenant-a",
            "repository": "org/repo",
            "pull_request": 7,
            "report": {"summary": "done"},
            "input": {"github_issue_url": "https://api.github.com/repos/org/repo/issues/7"},
        }
        store.resume_review_delivery.return_value = {"status": "resumed"}
        notify = mock.Mock()
        reviews = ReviewUseCases(
            store,
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            mock.Mock(),
            lambda: mock.Mock(),
            notify,
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: mock.Mock(),
            ReviewOptions(10_000, 60, True),
        )

        result = reviews.resume_task("task-1", "tenant-a", "alice")

        self.assertTrue(result["delivery_resumed"])
        store.resume_review_delivery.assert_called_once()
        self.assertEqual("alice", store.resume_review_delivery.call_args.args[-1])
        store.resume_review_task.assert_not_called()
        store.get_task_payload.assert_not_called()
        notify.assert_called_once()


class SessionCompletionTests(unittest.TestCase):
    def test_duplicate_delivery_counts_the_turn_once(self):
        store = mock.Mock()
        store.previous_open_snapshot.return_value = []
        store.complete_session_turn.side_effect = [True, False]
        sessions = SessionUseCases(store, max_diff_bytes=10_000)
        payload = {
            "task_id": "task-1",
            "repository": "org/repo",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "head_sha": "sha-1",
        }
        report = ReviewReport("org/repo", 7, "done", "low", [])
        captured = Metrics()

        with mock.patch("evoagent.application.sessions.metrics", captured):
            sessions.record_review_turn(payload, report)
            sessions.record_review_turn(payload, report)

        self.assertIn("evoagent_session_turns_total 1.0", captured.prometheus())


class ApplicationUseCaseTests(unittest.TestCase):
    def setUp(self):
        self.store = postgres_store(self)

    def test_session_use_case_owns_turn_continuity(self):
        sessions = SessionUseCases(self.store, max_diff_bytes=10_000)
        turn = self.store.start_session_turn("tenant", "org/repo", 7, "sha-1", "opened")
        report = ReviewReport("org/repo", 7, "one issue", "high", [_finding()])

        note = sessions.record_review_turn(
            {
                "task_id": "task-1",
                "repository": "org/repo",
                "session_id": turn["session_id"],
                "turn_id": turn["turn_id"],
                "head_sha": "sha-1",
            },
            report,
        )

        self.assertEqual("", note)
        timeline = sessions.get_for_pull_request("org/repo", 7, "tenant")
        self.assertEqual(1, len(timeline["turns"]))
        self.assertEqual(1, timeline["turns"][0]["summary"]["new"])

    def test_github_installation_binding_cannot_move_between_tenants(self):
        self.store.bind_installation(77, "org", "tenant-a", "alice")
        self.store.bind_installation(77, "org-renamed", "tenant-a", "alice")

        with self.assertRaisesRegex(AccessDeniedError, "another tenant"):
            self.store.bind_installation(77, "org", "tenant-b", "bob")

        self.assertEqual("tenant-a", self.store.installation_tenant(77))
        events = self.store.list_audit("tenant-a")
        self.assertEqual(
            ["github.installation.bind", "github.installation.bind"],
            [event["action"] for event in events[:2]],
        )

    def test_policy_use_case_rejects_rules_missing_from_runtime(self):
        policies = RepositoryPolicyResolver(self.store)
        use_cases = PolicyUseCases(
            self.store,
            policies,
            lambda: ("SEC-YAML-LOAD",),
        )
        with self.assertRaisesRegex(ValueError, "unavailable fix rules"):
            use_cases.set_repository_policy(
                "tenant",
                "org/repo",
                {"auto_fix": True, "allowed_fix_rules": ["UNKNOWN-RULE"]},
                "alice",
            )

        saved = use_cases.set_repository_policy(
            "tenant",
            "org/repo",
            {"auto_fix": True, "allowed_fix_rules": ["SEC-YAML-LOAD"]},
            "alice",
        )
        current = use_cases.get_repository_policy("tenant", "org/repo")
        self.assertEqual(saved["version"], current["version"])
        self.assertEqual(["SEC-YAML-LOAD"], current["policy"]["allowed_fix_rules"])

    def test_session_use_case_validates_impact_payload_at_boundary(self):
        sessions = SessionUseCases(self.store, max_diff_bytes=10)
        with self.assertRaisesRegex(ValueError, "maximum analysable size"):
            sessions.analyze_impact({"app.py": "x" * 101}, ["app.py"])

    def test_repair_use_case_is_idempotent_and_policy_scoped(self):
        task_id = "repair-task"
        self.store.bind_installation(77, "org", "tenant", "alice")
        self.store.create(
            task_id,
            "org/repo",
            7,
            {"installation_id": 77, "head_sha": "reviewed-sha"},
            "tenant",
        )
        self.store.succeed(
            task_id,
            ReviewReport("org/repo", 7, "one issue", "high", [_finding()]),
            TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
        )
        policies = RepositoryPolicyResolver(self.store)
        policies.save(
            "tenant",
            "org/repo",
            {"auto_fix": True, "allowed_fix_rules": ["SEC-EVAL"]},
            "alice",
        )

        class Fixer:
            rule_ids = ("SEC-EVAL", "SEC-YAML-LOAD")
            calls = 0
            allowed = ()

            def create_fix_commits(self, *_args, **kwargs):
                self.calls += 1
                self.allowed = kwargs["allowed_rule_ids"]
                self.expected_source_sha = kwargs["expected_source_sha"]
                return {"branch": "evoagent/fix", "commits": [{"sha": "abc"}]}

        fixer = Fixer()
        installations = []
        repairs = RepairUseCases(
            self.store,
            policies,
            fixer,
            lambda installation_id: installations.append(installation_id) or object(),
            RepairOptions(
                max_diff_bytes=10_000,
                verify_timeout_seconds=10,
                container_image="python:3.12-slim",
                memory_mb=256,
                pids_limit=64,
                cpus=1.0,
                max_output_bytes=10_000,
                effect_lease_seconds=30,
            ),
        )

        captured = Metrics()
        with mock.patch("evoagent.application.repairs.metrics", captured):
            first = repairs.create_fix(task_id, tenant_id="tenant")
            second = repairs.create_fix(task_id, tenant_id="tenant")

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["branch"], second["branch"])
        self.assertEqual(first["commits"], second["commits"])
        self.assertEqual(1, fixer.calls)
        self.assertEqual(("SEC-EVAL",), fixer.allowed)
        self.assertEqual("reviewed-sha", fixer.expected_source_sha)
        self.assertEqual([77], installations)
        output = captured.prometheus()
        self.assertIn("evoagent_fix_attempts_total 1.0", output)
        self.assertIn("evoagent_fix_runs_total 1.0", output)
        self.assertIn("evoagent_fix_published_total 1.0", output)
        self.assertIn("evoagent_fix_commits_total 1.0", output)
        self.assertIn("evoagent_fix_idempotent_replays_total 1.0", output)

    def test_repair_metrics_distinguish_abstention_verification_and_failure(self):
        policies = RepositoryPolicyResolver(self.store)
        options = RepairOptions(10_000, 10, "python:3.12-slim", 256, 64, 1.0, 10_000, 30)
        outcomes = {
            "abstained": {"branch": None, "commits": [], "note": "none eligible"},
            "verification": {
                "branch": None,
                "commits": [],
                "verification": {"passed": False},
            },
        }

        class Fixer:
            rule_ids = ("SEC-EVAL",)

            def __init__(self, outcome=None, error=False):
                self.outcome = outcome
                self.error = error

            def create_fix_commits(self, *_args, **_kwargs):
                if self.error:
                    raise RuntimeError("publication failed")
                return self.outcome

        def completed_task(task_id):
            self.store.create(task_id, "org/repo", 7, {"head_sha": "reviewed-sha"}, "tenant")
            self.store.succeed(
                task_id,
                ReviewReport("org/repo", 7, "one issue", "high", [_finding()]),
                TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
            )

        captured = Metrics()
        with mock.patch("evoagent.application.repairs.metrics", captured):
            for name, outcome in outcomes.items():
                completed_task(name)
                RepairUseCases(
                    self.store,
                    policies,
                    Fixer(outcome),
                    lambda _installation_id: object(),
                    options,
                ).create_fix(name, tenant_id="tenant")
            completed_task("failed")
            with self.assertRaisesRegex(RuntimeError, "publication failed"):
                RepairUseCases(
                    self.store,
                    policies,
                    Fixer(error=True),
                    lambda _installation_id: object(),
                    options,
                ).create_fix("failed", tenant_id="tenant")

        output = captured.prometheus()
        self.assertIn("evoagent_fix_attempts_total 3.0", output)
        self.assertIn("evoagent_fix_runs_total 2.0", output)
        self.assertIn("evoagent_fix_abstained_total 1.0", output)
        self.assertIn("evoagent_fix_verification_blocked_total 1.0", output)
        self.assertIn("evoagent_fix_failed_total 1.0", output)

    def test_review_use_case_owns_admission_and_execution(self):
        report = ReviewReport("org/repo", 7, "one issue", "high", [_finding()])

        class Queue:
            backend = "test"

        def execute(task_id, _repository, _pull_request, _diff, _tenant_id, _execution_generation):
            self.store.succeed(
                task_id,
                report,
                TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
            )
            return report

        reviews = ReviewUseCases(
            self.store,
            RepositoryPolicyResolver(self.store),
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: Queue(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: object(),
            ReviewOptions(
                max_diff_bytes=10_000,
                effect_lease_seconds=60,
                auto_post_review=False,
            ),
        )

        result = reviews.create_review(
            "org/repo", "--- a/a.py\n+++ b/a.py\n", 7, tenant_id="tenant"
        )

        task = self.store.get(result["task_id"], "tenant")
        self.assertEqual("SUCCESS", result["state"])
        self.assertEqual(0, task["input"]["repository_policy"]["version"])
        self.assertTrue(task["input"]["repository_policy"]["policy"]["enabled"])
        self.assertEqual(0, self.store.tenant_review_admission_stats("tenant")["active"])

    def test_review_use_case_fetches_deferred_diff_and_deduplicates_comment(self):
        report = ReviewReport("org/repo", 7, "one issue", "high", [_finding()])

        class Queue:
            backend = "test"

        class CodeHost:
            fetches = 0
            comments = 0

            def ensure_repository_access(self, repository):
                self.repository = repository

            def fetch_diff(self, _url, max_bytes=None):
                self.fetches += 1
                self.max_bytes = max_bytes
                return "--- a/a.py\n+++ b/a.py\n"

            def upsert_comment(self, _url, _body, _marker, before_write=None):
                if before_write is not None:
                    before_write()
                self.comments += 1

        code_host = CodeHost()
        executions = []

        def execute(task_id, _repository, _pull_request, _diff, _tenant_id, _execution_generation):
            executions.append(task_id)
            self.store.succeed(
                task_id,
                report,
                TraceEvent(1, TaskState.SUCCESS, "done", utc_now()),
            )
            return report

        reviews = ReviewUseCases(
            self.store,
            RepositoryPolicyResolver(self.store),
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: Queue(),
            lambda: None,
            lambda: ("local-rules", "local", ""),
            execute,
            lambda *_args: None,
            lambda *_args: "continuity",
            lambda _installation_id: code_host,
            ReviewOptions(10_000, 60, True),
        )
        payload = {
            "repository": "org/repo",
            "pull_request": 7,
            "tenant_id": "tenant",
            "diff_url": "https://example.invalid/pull.diff",
            "github_issue_url": "https://api.example.invalid/issues/7",
        }
        task_id = reviews.create_deferred_task(
            "org/repo",
            7,
            "github-webhook",
            "tenant",
            {"diff_url": payload["diff_url"]},
            payload,
        )
        queued = {"task_id": task_id, "admission_generation": 1, **payload}

        reviews.process_queued(queued)
        reviews.process_queued(queued)

        self.assertEqual(1, code_host.fetches)
        self.assertEqual(1, code_host.comments)
        self.assertEqual([task_id], executions)
        self.assertEqual("org/repo", code_host.repository)
        self.assertEqual(14_096, code_host.max_bytes)
        task = self.store.get(task_id, "tenant")
        self.assertFalse(task["input"]["diff_pending"])
        self.assertIsNotNone(self.store.get_task_payload(task_id))

    def test_resume_reacquires_capacity_with_transactional_outbox(self):
        class Queue:
            backend = "test"

            @staticmethod
            def submit(*_args, **_kwargs):
                raise AssertionError("resume must publish through the transactional outbox")

        notifications = []
        reviews = ReviewUseCases(
            self.store,
            RepositoryPolicyResolver(self.store),
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: Queue(),
            lambda: notifications.append("ready"),
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: object(),
            ReviewOptions(10_000, 60, False, tenant_max_active_reviews=1),
        )
        task_id = "resume-task"
        self.store.create_review_task(
            task_id,
            "org/repo",
            7,
            {"source": "api"},
            "tenant",
            "--- a/a.py\n+++ b/a.py\n+value = 1\n",
        )
        self.store.fail(
            task_id,
            "review execution failed [type=unknown; ref=2222222222222222]",
            TraceEvent(1, TaskState.FAILED, "failed", utc_now()),
        )

        resumed = reviews.resume_task(task_id, "tenant")
        duplicate = reviews.resume_task(task_id, "tenant")

        self.assertTrue(resumed["resumed"])
        self.assertFalse(resumed["already_active"])
        self.assertFalse(duplicate["resumed"])
        self.assertTrue(duplicate["already_active"])
        self.assertEqual(["ready"], notifications)
        outbox = self.store.list_outbox("pending", 10)
        resumed_message = next(item for item in outbox if item["id"].startswith("review-resume:"))
        self.assertEqual(2, resumed_message["payload"]["admission_generation"])
        dead_letter_error = "task delivery failed [type=unknown; ref=3333333333333333]"
        reviews.on_dead_letter(
            {"task_id": task_id, "tenant_id": "tenant", "admission_generation": 1},
            dead_letter_error,
        )
        self.assertEqual(1, self.store.tenant_review_admission_stats("tenant")["active"])
        self.assertEqual(TaskState.PENDING.value, self.store.get(task_id, "tenant")["state"])
        reviews.on_dead_letter(
            {"task_id": task_id, "tenant_id": "tenant", "admission_generation": 2},
            dead_letter_error,
        )
        self.assertEqual(0, self.store.tenant_review_admission_stats("tenant")["active"])
        self.assertEqual(TaskState.FAILED.value, self.store.get(task_id, "tenant")["state"])

    def test_webhook_use_case_creates_one_durable_review_unit(self):
        self.store.bind_installation(77, "org", "default", "alice")

        class Queue:
            backend = "test"

        queue = Queue()
        policies = RepositoryPolicyResolver(self.store)
        reviews = ReviewUseCases(
            self.store,
            policies,
            ReleaseManager(self.store),
            AlertManager(self.store),
            Observability(),
            lambda: queue,
            lambda: None,
            lambda: ("local-rules", "local", ""),
            lambda *_args: None,
            lambda *_args: None,
            lambda *_args: "",
            lambda _installation_id: object(),
            ReviewOptions(10_000, 60, True),
        )
        notifications = []
        webhooks = WebhookUseCases(
            self.store,
            reviews,
            lambda: queue,
            lambda: notifications.append("ready"),
            WebhookOptions("default", True),
        )
        payload = {
            "action": "opened",
            "number": 7,
            "installation": {"id": 77},
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "diff_url": "https://github.com/org/repo/pull/7.diff",
                "issue_url": "https://api.github.com/repos/org/repo/issues/7",
                "head": {"sha": "head-1"},
                "updated_at": "2026-08-23T12:00:00Z",
            },
        }

        first = webhooks.handle_github_pull_request(payload, "delivery-1", "payload-hash")
        duplicate = webhooks.handle_github_pull_request(payload, "delivery-1", "payload-hash")

        self.assertEqual("PENDING", first["state"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["task_id"], duplicate["task_id"])
        self.assertEqual(["ready"], notifications)
        task = self.store.get(first["task_id"], "default")
        self.assertEqual(first["session_id"], task["input"]["session_id"])
        self.assertEqual(77, task["input"]["installation_id"])
        self.assertEqual(first["task_id"], self.store.get_webhook("delivery-1")["task_id"])

        self.store.claim_webhook("legacy-unbound", "default", "pull_request", "legacy-hash")
        recovered = webhooks.handle_github_pull_request(payload, "legacy-unbound", "legacy-hash")
        self.assertNotIn("duplicate", recovered)
        self.assertEqual(recovered["task_id"], self.store.get_webhook("legacy-unbound")["task_id"])

        ready_payload = {
            **payload,
            "action": "ready_for_review",
            "pull_request": {
                **payload["pull_request"],
                "draft": False,
                "head": {"sha": "head-2"},
            },
        }
        ready = webhooks.handle_github_pull_request(ready_payload, "delivery-ready", "ready-hash")
        self.assertEqual(first["session_id"], ready["session_id"])
        self.assertEqual(
            "ready_for_review", self.store.get(ready["task_id"], "default")["input"]["trigger"]
        )

    def test_invalid_webhook_does_not_poison_delivery_id(self):
        class StubReviews:
            pass

        class Queue:
            backend = "test"

        webhooks = WebhookUseCases(
            self.store,
            StubReviews(),
            lambda: Queue(),
            lambda: None,
            WebhookOptions("default", False),
        )
        with self.assertRaisesRegex(ValueError, "invalid GitHub"):
            webhooks.handle_github_pull_request(
                {"action": "opened", "number": 7}, "bad-delivery", "hash"
            )
        self.assertIsNone(self.store.get_webhook("bad-delivery"))


if __name__ == "__main__":
    unittest.main()
