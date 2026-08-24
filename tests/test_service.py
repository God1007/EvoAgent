import tempfile
import threading
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

from evoagent.agents import MAX_REVIEW_AGENTS
from evoagent.config import Settings
from evoagent.diff_parser import parse_unified_diff
from evoagent.errors import ClientInputError
from evoagent.metrics import Metrics
from evoagent.model_gateway import ModelGateway
from evoagent.models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from evoagent.review_engine import ReviewEngine, _source_revision
from evoagent.review_extensions import ReviewerContribution
from evoagent.reviewer import GatewayReviewer, LocalRuleReviewer, Reviewer
from evoagent.service import ReviewService
from evoagent.task_queue import PermanentTaskError
from evoagent.time_utils import utc_now
from tests.db_support import postgres_url, reset_postgres


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


class ReadinessCacheTests(unittest.TestCase):
    def test_concurrent_cache_miss_runs_one_dependency_probe(self):
        service = ReviewService.__new__(ReviewService)
        service._readiness_lock = threading.Lock()
        service._readiness_cache = None
        service._readiness_ttl = 1.0
        first_probe = threading.Event()
        second_probe = threading.Event()
        release = threading.Event()
        calls = 0

        def compute():
            nonlocal calls
            calls += 1
            (first_probe if calls == 1 else second_probe).set()
            release.wait(5)
            return True, {"status": "ready"}

        service._compute_readiness = compute
        first = threading.Thread(target=service.readiness)
        second = threading.Thread(target=service.readiness)
        first.start()
        self.assertTrue(first_probe.wait(1))
        second.start()
        try:
            self.assertFalse(second_probe.wait(0.2))
        finally:
            release.set()
            first.join(1)
            second.join(1)

        self.assertEqual(1, calls)

    def test_degraded_downstreams_are_visible_without_removing_healthy_replicas(self):
        service = ReviewService.__new__(ReviewService)
        service.store = mock.Mock()
        service.store.schema_version.return_value = 1
        service.github_breaker = mock.Mock(state="open")
        service.llm_breaker = mock.Mock(state="half_open")
        service.review_engine = mock.Mock()
        service.review_engine.execution_revision.return_value = "reviewer-revision"
        service.repair_container_image = "sha256:" + "a" * 64
        service.settings = mock.Mock(repair_test_command="pytest -q")
        service.queue = mock.Mock(backend="redis")
        service.queue.health.return_value = {"healthy": True, "last_error": ""}
        service.queue.depth.return_value = 0
        service.outbox = mock.Mock()
        service.outbox.stats.return_value = {
            "dispatcher_running": True,
            "pending": 0,
            "publishing": 0,
            "dead": 1,
            "last_error": "",
        }

        ready, detail = service._compute_readiness()

        self.assertTrue(ready)
        service.store.ping.assert_not_called()
        self.assertEqual(1, detail["checks"]["outbox"]["dead"])
        self.assertEqual(
            {"github": "open", "llm": "half_open"},
            detail["checks"]["circuit_breakers"],
        )
        self.assertEqual("reviewer-revision", detail["reviewer_revision"])
        self.assertEqual(
            {"configured": True, "mode": "docker"},
            detail["checks"]["proof"],
        )
        self.assertEqual(
            {
                "configured": True,
                "mode": "docker",
                "test_command_configured": True,
            },
            detail["checks"]["repair"],
        )

        service.settings.repair_test_command = ""
        ready, detail = service._compute_readiness()
        self.assertTrue(ready)
        self.assertEqual(
            {
                "configured": False,
                "mode": "disabled",
                "test_command_configured": False,
            },
            detail["checks"]["repair"],
        )

        service.outbox.stats.return_value["last_error"] = "redis password leaked"
        ready, detail = service._compute_readiness()
        self.assertFalse(ready)
        self.assertRegex(
            detail["checks"]["outbox"]["last_error"],
            r"^outbox dispatch failed \[type=unknown; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn("password", str(detail))

        service.outbox.stats.return_value["last_error"] = ""
        service.queue.health.side_effect = RuntimeError("redis://queue-secret")
        ready, detail = service._compute_readiness()
        self.assertFalse(ready)
        self.assertEqual(-1, detail["queue_depth"])
        self.assertRegex(
            detail["checks"]["queue"]["last_error"],
            r"^queue dependency failed \[type=builtins.RuntimeError; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn("queue-secret", str(detail))


class ShutdownBudgetTests(unittest.TestCase):
    @staticmethod
    def service():
        service = ReviewService.__new__(ReviewService)
        service._closed = False
        service.settings = mock.Mock(queue_shutdown_timeout_seconds=30)
        service.retention = mock.Mock()
        service.outbox = mock.Mock()
        service.queue = mock.Mock()
        service.components = mock.Mock()
        service.retention.close.return_value = True
        service.outbox.close.return_value = True
        service.queue.close.return_value = True
        return service

    def test_components_share_one_total_drain_budget(self):
        service = self.service()

        with (
            mock.patch("evoagent.service.time.monotonic", side_effect=(100, 101, 110, 129)),
            mock.patch("evoagent.service.close_components") as close_components,
        ):
            service.close()

        service.retention.close.assert_called_once_with(29)
        service.outbox.close.assert_called_once_with(20)
        service.queue.close.assert_called_once_with(1)
        close_components.assert_called_once_with(service.components)

    def test_later_resources_stop_but_dependencies_stay_open_when_close_fails(self):
        service = self.service()
        service.retention.close.side_effect = RuntimeError("retention close failed")

        with (
            mock.patch("evoagent.service.close_components") as close_components,
            self.assertRaisesRegex(RuntimeError, "retention close failed"),
        ):
            service.close()

        service.outbox.close.assert_called_once()
        service.queue.close.assert_called_once()
        close_components.assert_not_called()

    def test_dependencies_stay_open_when_background_drain_times_out(self):
        service = self.service()
        service.queue.close.return_value = False

        with mock.patch("evoagent.service.close_components") as close_components:
            service.close()

        close_components.assert_not_called()
        self.assertFalse(service._closed)

    def test_close_retries_after_a_drain_timeout(self):
        service = self.service()
        service.queue.close.side_effect = [False, True]

        with mock.patch("evoagent.service.close_components") as close_components:
            service.close()
            service.close()

        close_components.assert_called_once_with(service.components)
        self.assertTrue(service._closed)

    def test_partial_initialization_keeps_dependencies_open_when_queue_does_not_stop(self):
        settings = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=10_000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
        )
        components = mock.Mock()
        components.review_engine.llm_config = {}
        queue = mock.Mock()
        queue.close.return_value = False
        outbox = mock.Mock()
        outbox.close.return_value = True

        with (
            mock.patch("evoagent.service.build_components", return_value=components),
            mock.patch("evoagent.service.TaskQueue", return_value=queue),
            mock.patch("evoagent.service.OutboxDispatcher", return_value=outbox),
            mock.patch(
                "evoagent.service.RetentionManager", side_effect=RuntimeError("startup failed")
            ),
            mock.patch("evoagent.service.close_components") as close_components,
            self.assertRaisesRegex(RuntimeError, "startup failed"),
        ):
            ReviewService(settings)

        outbox.close.assert_called_once()
        queue.close.assert_called_once()
        close_components.assert_not_called()


class OperationalMutationTests(unittest.TestCase):
    def test_outbox_replay_rejects_unbounded_id_before_store(self):
        service = ReviewService.__new__(ReviewService)
        service.store = mock.Mock()

        for message_id in ("", "x" * 201):
            with self.subTest(message_id=message_id[:20]), self.assertRaises(ClientInputError):
                service.replay_outbox(message_id, "tenant-a", "alice")

        service.store.requeue_outbox.assert_not_called()

    def test_outbox_replay_preserves_actor_and_notifies_only_after_commit(self):
        for replayed in (False, True):
            with self.subTest(replayed=replayed):
                service = ReviewService.__new__(ReviewService)
                service.store = mock.Mock()
                service.store.requeue_outbox.return_value = replayed
                service.outbox = mock.Mock()

                self.assertEqual(
                    replayed,
                    service.replay_outbox("review:task-1", "tenant-a", "alice"),
                )

                service.store.requeue_outbox.assert_called_once_with(
                    "review:task-1", "tenant-a", "alice"
                )
                if replayed:
                    service.outbox.notify.assert_called_once_with()
                else:
                    service.outbox.notify.assert_not_called()

    def test_task_cancel_preserves_actor_through_the_use_case(self):
        service = ReviewService.__new__(ReviewService)
        service.review_use_cases = mock.Mock()
        result = {"accepted": True, "cancel_requested": True, "state": "CANCELLED"}
        service.review_use_cases.cancel_task.return_value = result

        self.assertEqual(result, service.cancel_task("task-1", "tenant-a", "alice"))

        service.review_use_cases.cancel_task.assert_called_once_with("task-1", "tenant-a", "alice")

    def test_task_resume_preserves_actor_through_the_use_case(self):
        service = ReviewService.__new__(ReviewService)
        service.review_use_cases = mock.Mock()
        service.review_use_cases.resume_task.return_value = {"resumed": True}

        self.assertEqual({"resumed": True}, service.resume_task("task-1", "tenant-a", "alice"))

        service.review_use_cases.resume_task.assert_called_once_with("task-1", "tenant-a", "alice")


class ExecutionSourceRevisionTests(unittest.TestCase):
    def test_evaluation_reviewer_reuses_the_production_evidence_gate(self):
        class UnanchoredReviewer(Reviewer):
            name = "unanchored-reviewer"

            def review(self, _diff, parsed):
                line = parsed.added_lines[0]
                return [
                    Finding(
                        "SEC-EVAL",
                        Severity.HIGH,
                        "Dangerous dynamic execution",
                        "The reported evidence does not match the changed line.",
                        line.path,
                        line.line,
                        "not present in the diff",
                        "Replace dynamic execution with an explicit parser.",
                        "Assert hostile input cannot execute as code.",
                        0.9,
                    )
                ]

        engine = ReviewEngine.__new__(ReviewEngine)
        engine.settings = mock.Mock(timeout_seconds=1)
        engine.store = mock.Mock()
        engine.build_llm_reviewer = mock.Mock(return_value=UnanchoredReviewer())
        reviewer = engine.build_evaluation_reviewer("candidate prompt")
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+eval(data)\n"

        self.assertEqual([], reviewer.review(diff, parse_unified_diff(diff)))
        engine.build_llm_reviewer.assert_called_once_with("candidate prompt")
        engine.store.record_agent_message.assert_not_called()

    def test_trusted_contributions_are_bounded_before_registration(self):
        contributions = tuple(
            ReviewerContribution("reviewer-%d" % index, LocalRuleReviewer())
            for index in range(MAX_REVIEW_AGENTS + 1)
        )

        with self.assertRaisesRegex(ValueError, "at most %d" % MAX_REVIEW_AGENTS):
            ReviewEngine(
                mock.Mock(),
                mock.Mock(),
                mock.Mock(),
                ModelGateway(None, None),
                contributions,
            )

    def test_duplicate_contribution_ids_fail_before_registration(self):
        contribution = ReviewerContribution("duplicate", LocalRuleReviewer())

        with self.assertRaisesRegex(ValueError, "duplicate reviewer contribution ids"):
            ReviewEngine(
                mock.Mock(),
                mock.Mock(),
                mock.Mock(),
                ModelGateway(None, None),
                (contribution, contribution),
            )

    def test_model_context_rejects_an_incomplete_policy_snapshot(self):
        engine = ReviewEngine.__new__(ReviewEngine)
        engine.store = mock.Mock()
        for snapshot in ({"version": 1}, {"policy": {}}):
            engine.store.get.return_value = {
                "tenant_id": "tenant",
                "repository": "org/repo",
                "input": {"repository_policy": snapshot},
            }

            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                engine._task_context("task")

    def test_source_revision_is_path_independent_and_changes_with_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = [Path(temporary) / name for name in ("first", "second")]
            for root in roots:
                (root / "nested").mkdir(parents=True)
                (root / "main.py").write_text("value = 1\n", encoding="utf-8")
                (root / "nested" / "worker.py").write_text("result = 2\n", encoding="utf-8")

            original = _source_revision(roots[0])
            self.assertEqual(original, _source_revision(roots[1]))
            (roots[1] / "main.py").write_text("value = 3\n", encoding="utf-8")
            self.assertNotEqual(original, _source_revision(roots[1]))

    def test_execution_revision_binds_harness_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                host="127.0.0.1",
                port=8080,
                max_diff_bytes=10000,
                max_steps=8,
                timeout_seconds=10,
                llm_base_url="",
                llm_api_key="",
                llm_model="",
                github_webhook_secret="",
                github_token="",
                auto_post_review=False,
                skills_dir=temporary,
            )

            def revision(config):
                return ReviewEngine(
                    config,
                    mock.Mock(),
                    mock.Mock(),
                    ModelGateway(None, None),
                    (ReviewerContribution("local", LocalRuleReviewer()),),
                ).execution_revision()

            original = revision(settings)
            self.assertNotEqual(original, revision(replace(settings, max_steps=9)))
            self.assertNotEqual(original, revision(replace(settings, timeout_seconds=11)))


class TenantDeadLetterTests(unittest.TestCase):
    def test_uses_persisted_task_tenant_instead_of_queue_payload(self):
        service = ReviewService.__new__(ReviewService)
        service.queue = mock.Mock()
        service.queue.dead_letters.return_value = [
            {"message_id": "task-a", "payload": {"task_id": "task-a", "tenant_id": "forged"}},
            {
                "message_id": "task-b",
                "payload": {"task_id": "task-b", "tenant_id": "tenant-a"},
            },
        ]
        service.store = mock.Mock()
        service.store.tenant_task_ids.return_value = {"task-a"}

        messages = service.tenant_dead_letters("tenant-a", 100)

        self.assertEqual(["task-a"], [item["message_id"] for item in messages])
        service.store.tenant_task_ids.assert_called_once_with("tenant-a", ["task-a", "task-b"])
        service.store.get.assert_not_called()


class RuntimeSkillReloadTests(unittest.TestCase):
    def test_runtime_views_are_resolved_from_the_engine_snapshot(self):
        service = ReviewService.__new__(ReviewService)
        service.review_engine = mock.Mock()
        first_reviewer = mock.Mock()
        first_harness = mock.Mock(reviewer=first_reviewer)
        service.review_engine.execution_snapshot.return_value = (
            "first",
            (),
            first_harness,
        )

        self.assertIs(first_reviewer, service.reviewer)
        self.assertIs(first_harness, service.harness)

        second_reviewer = mock.Mock()
        second_harness = mock.Mock(reviewer=second_reviewer)
        service.review_engine.execution_snapshot.return_value = (
            "second",
            (),
            second_harness,
        )

        self.assertIs(second_reviewer, service.reviewer)
        self.assertIs(second_harness, service.harness)

    def test_skill_inventory_delegates_to_the_atomic_engine_snapshot(self):
        service = ReviewService.__new__(ReviewService)
        service.review_engine = mock.Mock()
        service.review_engine.inventory_snapshot.return_value = ([{"name": "security"}], "r1")

        self.assertEqual(([{"name": "security"}], "r1"), service.skill_inventory())


class GitHubClientFactoryTests(unittest.TestCase):
    def test_installation_token_refresh_uses_the_shared_github_breaker(self):
        service = ReviewService.__new__(ReviewService)
        service.settings = mock.Mock(
            github_app_id="app",
            github_app_slug="evoagent",
            github_private_key_path="key.pem",
            repair_memory_mb=128,
        )
        service.github_breaker = mock.Mock()
        authenticator = mock.Mock()
        authenticator.installation_token.return_value = "token"

        with (
            mock.patch(
                "evoagent.service.GitHubAppAuthenticator", return_value=authenticator
            ) as cls,
            mock.patch("evoagent.service.GitHubClient") as client,
        ):
            service.github_client_for_installation(77)

        cls.assert_called_once_with("app", "key.pem", breaker=service.github_breaker)
        client.assert_called_once()
        self.assertEqual(128 * 1024 * 1024, client.call_args.kwargs["max_archive_bytes"])
        self.assertEqual("evoagent[bot]", client.call_args.kwargs["comment_author_login"])


class RolloutExecutionBindingTests(unittest.TestCase):
    def test_shadow_compares_the_same_evidence_graph_without_persisting_messages(self):
        service = ReviewService.__new__(ReviewService)
        candidate = mock.Mock(spec=GatewayReviewer)
        current_model = mock.Mock(spec=GatewayReviewer)
        local = LocalRuleReviewer()
        shadow = mock.Mock()
        finding = _finding()
        shadow.review_with_context.return_value = [finding]
        service.store = mock.Mock()
        service.store.get.return_value = {
            "repository": "org/repo",
            "input": {
                "shadow": True,
                "release_lane": "stable",
                "release_candidate_version": 2,
                "release_generation": 3,
            },
        }
        service.store.get_deployment.return_value = {
            "status": "running",
            "candidate_version": 2,
            "generation": 3,
        }
        service.policies = mock.Mock()
        service.policies.resolve.return_value = mock.Mock(enabled=True)
        service._versioned_reviewer = mock.Mock(return_value=candidate)
        service.review_engine = mock.Mock()
        service.review_engine.execution_snapshot.return_value = (
            "revision",
            (local, current_model),
            mock.Mock(),
        )
        service.review_engine.build_coordinator.return_value = shadow
        service.releases = mock.Mock()

        service._run_shadow(
            "task",
            "tenant-a",
            "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n",
            ReviewReport("org/repo", 7, "primary", "low", [finding]),
        )

        service.review_engine.build_coordinator.assert_called_once_with(
            [local, candidate], persist_messages=False
        )
        shadow.review_with_context.assert_called_once()
        observation = service.releases.observe_shadow.call_args
        candidate_payload = observation.args[5]
        self.assertEqual([finding.fingerprint()], candidate_payload["finding_keys"])
        self.assertEqual(
            ("shadow.completed", {"findings": 1, "candidate_output_used": False}),
            observation.kwargs["audit_event"],
        )
        service.store.audit.assert_not_called()

    def test_canary_shadow_runs_stable_and_records_baseline_before_candidate(self):
        service = ReviewService.__new__(ReviewService)
        stable = mock.Mock(spec=GatewayReviewer)
        current_model = mock.Mock(spec=GatewayReviewer)
        shadow = mock.Mock()
        stable_finding = _finding(rule_id="STABLE")
        candidate_finding = _finding(rule_id="CANDIDATE")
        shadow.review_with_context.return_value = [stable_finding]
        service.store = mock.Mock()
        service.store.get.return_value = {
            "repository": "org/repo",
            "input": {
                "shadow": True,
                "release_lane": "canary",
                "release_stable_version": 1,
                "release_candidate_version": 2,
                "release_generation": 3,
            },
        }
        service.store.get_deployment.return_value = {
            "status": "running",
            "candidate_version": 2,
            "generation": 3,
        }
        service.policies = mock.Mock()
        service.policies.resolve.return_value = mock.Mock(enabled=True)
        service._versioned_reviewer = mock.Mock(return_value=stable)
        service.review_engine = mock.Mock()
        service.review_engine.execution_snapshot.return_value = (
            "revision",
            (LocalRuleReviewer(), current_model),
            mock.Mock(),
        )
        service.review_engine.build_coordinator.return_value = shadow
        service.releases = mock.Mock()

        service._run_shadow(
            "task",
            "tenant-a",
            "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+value = 1\n",
            ReviewReport("org/repo", 7, "candidate", "low", [candidate_finding]),
        )

        service._versioned_reviewer.assert_called_once_with(1)
        observed = service.releases.observe_shadow.call_args.args
        self.assertEqual([stable_finding.fingerprint()], observed[4]["finding_keys"])
        self.assertEqual([candidate_finding.fingerprint()], observed[5]["finding_keys"])

    def test_repository_kill_switch_blocks_shadow_model_egress(self):
        service = ReviewService.__new__(ReviewService)
        service.store = mock.Mock()
        service.store.get.return_value = {
            "repository": "org/repo",
            "input": {"shadow": True},
        }
        service.policies = mock.Mock()
        service.policies.resolve.return_value = mock.Mock(enabled=False)
        service._versioned_reviewer = mock.Mock()

        service._run_shadow("task", "tenant-a", "diff", _Report([]))

        service.store.get_deployment.assert_not_called()
        service._versioned_reviewer.assert_not_called()
        self.assertEqual(
            {"reason": "repository was disabled after task acceptance"},
            service.store.audit.call_args.args[-1],
        )

    def test_invalid_release_snapshot_cannot_select_a_prompt(self):
        service = ReviewService.__new__(ReviewService)
        service.store = mock.Mock()
        harness = mock.Mock()
        service.review_engine = mock.Mock()
        service.review_engine.execution_snapshot.return_value = (
            "reviewer-revision",
            (),
            harness,
        )
        baseline = {
            "reviewer_revision": "reviewer-revision",
            "release_lane": "canary",
            "shadow": False,
            "release_stable_version": 2,
            "release_candidate_version": 1,
            "release_generation": 1,
        }

        for override in (
            {"release_lane": "unknown"},
            {"release_candidate_version": True},
            {"shadow": "false"},
        ):
            service.store.get.return_value = {"input": {**baseline, **override}}
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(PermanentTaskError, "release snapshot"),
            ):
                service._run_review("task", "org/repo", 7, "diff", "tenant-a")

        service.store.get_deployment.assert_not_called()
        harness.run.assert_not_called()

    def test_reconfigured_rollout_cannot_run_or_observe_the_stale_candidate(self):
        service = ReviewService.__new__(ReviewService)
        service.llm_config = {"provider": "test"}
        service.store = mock.Mock()
        service.store.get.return_value = {
            "repository": "org/repo",
            "input": {
                "reviewer_revision": "reviewer-revision",
                "release_lane": "canary",
                "shadow": True,
                "release_stable_version": 1,
                "release_candidate_version": 2,
                "release_generation": 1,
            },
        }
        service.store.get_deployment.return_value = {
            "status": "running",
            "stable_version": 2,
            "candidate_version": 2,
            "generation": 2,
        }
        service.store.get_skill_version.return_value = {
            "version": 1,
            "prompt": "stable-one",
        }
        service._build_llm_reviewer = mock.Mock(return_value=mock.Mock())
        service.review_engine = mock.Mock()
        service.review_engine.build_coordinator.return_value = mock.Mock()
        versioned_harness = mock.Mock()
        versioned_harness.run.return_value = "stable-report"
        service.review_engine.build_harness.return_value = versioned_harness
        default_harness = mock.Mock()
        service.review_engine.execution_snapshot.return_value = (
            "reviewer-revision",
            (),
            default_harness,
        )
        service.releases = mock.Mock()
        service.policies = mock.Mock()
        service.policies.resolve.return_value = mock.Mock(enabled=True)

        result = service._run_review("task", "org/repo", 7, "diff", "tenant-a")
        service._run_shadow("task", "tenant-a", "diff", _Report([]))

        self.assertEqual("stable-report", result)
        service.store.get_skill_version.assert_called_once_with("llm-review", 1)
        service._build_llm_reviewer.assert_called_once_with("stable-one")
        service.releases.observe_shadow.assert_not_called()
        actions = [call.args[2] for call in service.store.audit.call_args_list]
        self.assertEqual(["canary.skipped", "shadow.skipped"], actions)

    def test_missing_assigned_version_fails_instead_of_using_the_default_reviewer(self):
        service = ReviewService.__new__(ReviewService)
        service.llm_config = {"provider": "test"}
        service.store = mock.Mock()
        service.store.get.return_value = {
            "input": {
                "reviewer_revision": "reviewer-revision",
                "release_lane": "canary",
                "shadow": False,
                "release_stable_version": 1,
                "release_candidate_version": 2,
                "release_generation": 1,
            }
        }
        service.store.get_deployment.return_value = {
            "status": "running",
            "stable_version": 1,
            "candidate_version": 2,
            "generation": 1,
        }
        service.store.get_skill_version.return_value = None
        default_harness = mock.Mock()
        service.review_engine = mock.Mock()
        service.review_engine.execution_snapshot.return_value = (
            "reviewer-revision",
            (),
            default_harness,
        )

        with self.assertRaisesRegex(RuntimeError, "assigned LLM review version"):
            service._run_review("task", "org/repo", 7, "diff", "tenant-a")

        default_harness.run.assert_not_called()

    def test_queued_task_cannot_resume_with_a_different_reviewer_revision(self):
        service = ReviewService.__new__(ReviewService)
        service.store = mock.Mock()
        service.store.get.return_value = {"input": {"reviewer_revision": "accepted"}}
        harness = mock.Mock()
        service.review_engine = mock.Mock()
        service.review_engine.execution_snapshot.return_value = ("current", (), harness)

        with self.assertRaisesRegex(PermanentTaskError, "reviewer revision"):
            service._run_review("task", "org/repo", 7, "diff", "tenant-a")

        harness.run.assert_not_called()
        self.assertEqual("reviewer.revision_mismatch", service.store.audit.call_args.args[2])

    def test_pre_binding_task_cannot_run_without_a_reviewer_revision(self):
        service = ReviewService.__new__(ReviewService)
        service.store = mock.Mock()
        service.store.get.return_value = {"input": {}}
        harness = mock.Mock()
        service.review_engine = mock.Mock()
        service.review_engine.execution_snapshot.return_value = ("current", (), harness)

        with self.assertRaisesRegex(PermanentTaskError, "reviewer revision"):
            service._run_review("task", "org/repo", 7, "diff", "tenant-a")

        harness.run.assert_not_called()
        self.assertIsNone(service.store.audit.call_args.args[4]["expected"])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.database_url = postgres_url(self)
        reset_postgres(self.database_url)
        self.settings = Settings(
            host="127.0.0.1",
            port=8080,
            max_diff_bytes=10000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
            database_url=self.database_url,
        )

    def test_fix_publication_result_is_cached_by_durable_effect_key(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.close)
        task_id = "fix-task"
        service.store.create(task_id, "org/repo", 7, {"head_sha": "reviewed-sha"}, "default")
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

        self.assertEqual(
            {"branch": "evoagent/fix", "commits": [{"sha": "abc"}], "replayed": False},
            first,
        )
        self.assertEqual({**first, "replayed": True}, second)
        self.assertEqual(1, fixer.calls)

    def test_feedback_metrics_use_fixed_category_names(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.close)
        service.store.create("feedback-task", "org/repo", 7, {}, "default")
        captured = Metrics()

        with mock.patch("evoagent.application.reviews.metrics", captured):
            for category in ("false_positive", "missed_issue", "bad_fix", "accepted"):
                service.record_feedback("feedback-task", category, None, "operator note")

        output = captured.prometheus()
        self.assertIn("evoagent_feedback_total 4.0", output)
        self.assertIn("evoagent_feedback_false_positive_total 1.0", output)
        self.assertIn("evoagent_feedback_missed_issue_total 1.0", output)
        self.assertIn("evoagent_feedback_bad_fix_total 1.0", output)
        self.assertIn("evoagent_feedback_accepted_total 1.0", output)

    def test_readiness_exposes_queue_worker_health_and_fails_closed(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.close)
        ready, detail = service.readiness()
        self.assertTrue(ready)
        self.assertTrue(detail["checks"]["queue"]["healthy"])

        service.queue.health = lambda: {  # type: ignore[method-assign]
            "healthy": False,
            "backend": service.queue.backend,
            "workers_running": 0,
            "workers_expected": 1,
            "last_error": "worker stopped",
        }
        service._readiness_cache = None
        ready, detail = service.readiness()
        self.assertFalse(ready)
        self.assertEqual("not-ready", detail["status"])
        self.assertRegex(
            detail["checks"]["queue"]["last_error"],
            r"^queue dependency failed \[type=unknown; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn("worker stopped", str(detail))

    def test_review_persists_versioned_repository_policy_snapshot(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.close)
        first = service.set_repository_policy(
            "default",
            "org/repo",
            {"max_diff_bytes": 10},
            "alice",
        )
        self.assertEqual(1, first["version"])
        diff = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+eval(data)\n"
        with self.assertRaisesRegex(ValueError, "repository policy limit"):
            service.create_review("org/repo", diff, 1)

        second = service.set_repository_policy(
            "default",
            "org/repo",
            {
                "max_diff_bytes": 1000,
                "allowed_reviewers": [service.reviewer.name],
                "allowed_llm_providers": ["local"],
            },
            "alice",
        )
        result = service.create_review("org/repo", diff, 1)
        task = service.store.get(result["task_id"], "default")
        snapshot = task["input"]["repository_policy"]
        self.assertEqual(2, second["version"])
        self.assertEqual(2, snapshot["version"])
        self.assertEqual([service.reviewer.name], snapshot["policy"]["allowed_reviewers"])

    def test_repository_policy_rejects_unknown_fix_rule_before_persisting(self):
        service = ReviewService(self.settings)
        self.addCleanup(service.close)
        with self.assertRaisesRegex(ValueError, "unavailable fix rules"):
            service.set_repository_policy(
                "default",
                "org/repo",
                {"allowed_fix_rules": ["UNKNOWN-RULE"]},
                "alice",
            )
        self.assertIsNone(service.store.get_repository_policy("default", "org/repo"))

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
        database_url = postgres_url(self)
        reset_postgres(database_url)
        self.service = ReviewService(
            Settings(
                host="127.0.0.1",
                port=8080,
                max_diff_bytes=10000,
                max_steps=8,
                timeout_seconds=10,
                llm_base_url="",
                llm_api_key="",
                llm_model="",
                github_webhook_secret="",
                github_token="",
                auto_post_review=False,
                database_url=database_url,
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
        result = self.service.provide_session_input(turn["session_id"], "prod", "default", "alice")
        self.assertEqual("open", result["status"])
        session = self.service.store.get_session("default", "org/repo", 7)
        self.assertEqual("open", session["status"])
        audit = next(
            item
            for item in self.service.store.list_audit("default")
            if item["action"] == "session.input.provided"
        )
        self.assertEqual("alice", audit["actor"])
        self.assertEqual({}, audit["detail"])

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
