import hashlib
import uuid
from typing import Any

from .agents import FilteredAgent, MultiAgentCoordinator
from .auth import AuthManager
from .codegraph import build_graph
from .config import Settings
from .diff_parser import parse_unified_diff
from .evolution import EvolutionEngine
from .fixer import SafeFixer
from .github import GitHubAppAuthenticator, GitHubClient
from .harness import ReviewHarness
from .metrics import metrics
from .models import TaskState, TraceEvent
from .observability import AlertManager, Observability
from .postgres_store import create_store
from .proof import ProofRunner
from .report import to_markdown
from .reviewer import LocalRuleReviewer, OpenAICompatibleReviewer
from .rollout import ReleaseManager
from .session import classify_findings, continuity_summary, open_snapshot
from .skills import SkillRegistry
from .store import utc_now
from .task_queue import PermanentTaskError, TaskQueue
from .verifier import RepairVerifier


class ReviewService:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.validate_evolution()
        self.llm_config = settings.resolved_llm()
        self.store = create_store(settings.database_url, settings.db_path)
        self.observability = Observability(settings.otel_service_name, settings.otel_endpoint)
        self.registry = SkillRegistry(
            settings.skills_dir,
            settings.skill_sandbox,
            settings.skill_timeout_seconds,
            settings.skill_memory_mb,
            settings.skill_signing_key,
            settings.skill_container_image,
        )
        local = LocalRuleReviewer()
        self.registry.register(
            "security-review",
            FilteredAgent("security-agent", local, ("SEC-",)),
            "1.0.0",
            "Security, injection and secret detection",
        )
        self.registry.register(
            "reliability-review",
            FilteredAgent("reliability-agent", local, ("REL-",)),
            "1.0.0",
            "Reliability and observability review",
        )
        if self.llm_config:
            active = self.store.get_active_skill_version("llm-review")
            self.registry.register(
                "llm-review",
                self._build_llm_reviewer(active["prompt"] if active else ""),
                "1.0.0",
                "Context-aware AI code review via %s" % self.llm_config["provider"],
            )
        self.registry.reload()
        coordinator = MultiAgentCoordinator(self.registry.reviewers(), store=self.store)
        self.reviewer = coordinator
        self.harness = ReviewHarness(
            self.store,
            self.reviewer,
            settings.max_steps,
            settings.timeout_seconds,
            observability=self.observability,
        )
        self.github = GitHubClient(settings.github_token)
        self.fixer = SafeFixer(
            RepairVerifier(
                settings.repair_test_command,
                settings.repair_verify_timeout_seconds,
                container_image=settings.repair_container_image,
                memory_mb=settings.repair_memory_mb,
                pids_limit=settings.repair_pids_limit,
                cpus=settings.repair_cpus,
                require_container=settings.repair_require_container,
                max_output_bytes=settings.repair_max_output_bytes,
            )
        )
        self.auth = AuthManager(
            self.store,
            settings.auth_secret,
            settings.session_ttl_seconds,
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password,
            settings.default_tenant_id,
        )
        self.releases = ReleaseManager(self.store)
        self.alerts = AlertManager(
            self.store, settings.alert_failure_rate, settings.alert_min_samples
        )
        self.evolution = EvolutionEngine(
            self.store,
            reviewer_factory=self._build_llm_reviewer if self.llm_config else None,
            min_cases=settings.eval_min_cases,
            max_cases=settings.eval_max_cases,
            min_improvement=settings.eval_min_improvement,
            min_holdout_cases=settings.eval_min_holdout_cases,
            max_metric_regression=settings.eval_max_metric_regression,
        )
        self.queue = TaskQueue(
            self._process_queued,
            settings.async_workers,
            settings.redis_url,
            settings.queue_max_attempts,
            settings.queue_lease_seconds,
            self._on_dead_letter,
        )

    def _build_llm_reviewer(self, prompt: str = "") -> OpenAICompatibleReviewer:
        if not self.llm_config:
            raise RuntimeError("no LLM provider is configured")
        return OpenAICompatibleReviewer(
            str(self.llm_config["base_url"]),
            str(self.llm_config["api_key"]),
            str(self.llm_config["model"]),
            self.settings.timeout_seconds,
            system_prompt=prompt,
            provider=str(self.llm_config["provider"]),
            extra_headers=dict(self.llm_config.get("headers") or {}),
        )

    def _candidate_reviewer(self, tenant_id: str):
        if not self.llm_config:
            return None
        deployment = self.store.get_deployment(tenant_id, "llm-review")
        if not deployment or deployment.get("candidate_version") is None:
            return None
        versions = self.store.list_skill_versions("llm-review")
        candidate = next(
            (
                item
                for item in versions
                if int(item["version"]) == int(deployment["candidate_version"])
            ),
            None,
        )
        return self._build_llm_reviewer(candidate["prompt"]) if candidate else None

    def _run_review(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        diff: str,
        tenant_id: str,
    ):
        task = self.store.get(task_id, tenant_id) or {}
        deployment = self.store.get_deployment(tenant_id, "llm-review")
        if (task.get("input") or {}).get("release_lane") == "canary" or (
            deployment and deployment.get("status") == "promoted"
        ):
            candidate = self._candidate_reviewer(tenant_id)
            if candidate:
                canary_reviewer = MultiAgentCoordinator(
                    [
                        item
                        for item in self.registry.reviewers()
                        if not isinstance(item, OpenAICompatibleReviewer)
                    ]
                    + [candidate],
                    store=self.store,
                )
                harness = ReviewHarness(
                    self.store,
                    canary_reviewer,
                    self.settings.max_steps,
                    self.settings.timeout_seconds,
                    observability=self.observability,
                )
                return harness.run(task_id, repository, pull_request, diff)
        return self.harness.run(task_id, repository, pull_request, diff)

    def _run_shadow(
        self,
        task_id: str,
        tenant_id: str,
        diff: str,
        primary_report,
    ) -> None:
        task = self.store.get(task_id, tenant_id) or {}
        if not (task.get("input") or {}).get("shadow"):
            return
        candidate = self._candidate_reviewer(tenant_id)
        if not candidate:
            self.store.audit(
                tenant_id,
                "system",
                "shadow.skipped",
                task_id,
                {"reason": "candidate reviewer is unavailable"},
            )
            return
        lane = (task.get("input") or {}).get("release_lane", "stable")
        primary: dict[str, object] = {
            "risk": primary_report.risk,
            "finding_keys": sorted(item.fingerprint() for item in primary_report.findings),
        }
        try:
            parsed = parse_unified_diff(diff)
            findings = candidate.review(diff, parsed)
            candidate_result: dict[str, object] = {
                "finding_keys": sorted(item.fingerprint() for item in findings)
            }
            rollout = self.releases.observe_shadow(
                tenant_id, "llm-review", task_id, lane, primary, candidate_result
            )
            self.store.audit(
                tenant_id,
                "system",
                "shadow.completed",
                task_id,
                {
                    "findings": len(findings),
                    "candidate_output_used": False,
                    "rollout_status": (rollout or {}).get("status"),
                },
            )
            metrics.inc("shadow_reviews_total")
        except Exception as exc:
            self.releases.observe_shadow(
                tenant_id, "llm-review", task_id, lane, primary, None, True
            )
            self.store.audit(
                tenant_id, "system", "shadow.failed", task_id, {"error": str(exc)[:500]}
            )
            metrics.inc("shadow_reviews_failed_total")

    def _record_session_turn(self, payload: dict[str, Any], report) -> str:
        """Classify this turn's findings for continuity and persist the snapshot.

        Returns a short markdown continuity footer for the PR comment, or an
        empty string when there is no session context or this is the first turn.
        """
        session_id = payload.get("session_id")
        turn_id = payload.get("turn_id")
        if not session_id or not turn_id:
            return ""
        repository = payload["repository"]
        previous = self.store.previous_open_snapshot(session_id, turn_id)
        classified = classify_findings(repository, previous, list(report.findings))
        summary = continuity_summary(classified)
        snapshots = open_snapshot(repository, classified)
        self.store.complete_session_turn(
            session_id,
            turn_id,
            payload.get("task_id"),
            snapshots,
            summary,
            payload.get("head_sha"),
        )
        metrics.inc("session_turns_total")
        if not previous:
            return ""
        return self._continuity_note(summary)

    @staticmethod
    def _continuity_note(summary: dict[str, int]) -> str:
        return "> **会话连续性** — 新增 %d · 仍存在 %d · 已修复 %d · 移动 %d（当前未解决 %d）" % (
            summary["new"],
            summary["still_open"],
            summary["resolved"],
            summary["moved"],
            summary["open"],
        )

    def get_session_timeline(
        self, session_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        return self.store.get_session_timeline(session_id, tenant_id)

    def get_session_for_pull_request(
        self, repository: str, pull_request: int, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        session = self.store.get_session(tenant_id, repository, pull_request)
        if not session:
            return None
        return self.store.get_session_timeline(session["id"], tenant_id)

    def provide_session_input(
        self, session_id: str, message: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        """Record a human reply to an input-required session and reopen it."""
        timeline = self.store.get_session_timeline(session_id, tenant_id)
        if timeline is None:
            raise ValueError("session not found")
        self.store.resolve_session_input(session_id)
        self.store.audit(
            tenant_id or timeline.get("tenant_id", "default"),
            "user",
            "session.input.provided",
            session_id,
            {"message": message[:2000]},
        )
        return {"session_id": session_id, "status": "open"}

    def analyze_impact(self, sources: dict[str, Any], changed_paths: list[Any]) -> dict[str, Any]:
        """Build a Python code graph from the supplied sources and return the
        blast radius of the changed files (impacted symbols + importing files)."""
        if not isinstance(sources, dict) or not isinstance(changed_paths, list):
            raise ValueError("'files' object and 'changed' list are required")
        clean = {
            path: text
            for path, text in sources.items()
            if isinstance(path, str) and isinstance(text, str)
        }
        if len(clean) > 5000:
            raise ValueError("too many files to analyse in a single request")
        total = sum(len(text.encode("utf-8")) for text in clean.values())
        if total > self.settings.max_diff_bytes * 10:
            raise ValueError("source payload exceeds the maximum analysable size")
        graph = build_graph(clean)
        return graph.impact_of([path for path in changed_paths if isinstance(path, str)])

    def _proof_verifier(self, command: str) -> RepairVerifier:
        # The proof path runs BOTH untrusted PR code and an operator/attacker
        # supplied command, so it is strictly more dangerous than a repo-test
        # run. Container isolation is therefore forced here regardless of the
        # global repair setting: without a configured image the run reports
        # ``error`` and the ladder stays at L1 rather than shelling out on host.
        return RepairVerifier(
            command,
            self.settings.repair_verify_timeout_seconds,
            container_image=self.settings.repair_container_image,
            memory_mb=self.settings.repair_memory_mb,
            pids_limit=self.settings.repair_pids_limit,
            cpus=self.settings.repair_cpus,
            require_container=True,
            max_output_bytes=self.settings.repair_max_output_bytes,
        )

    def run_proof(
        self,
        original_files: dict[str, Any],
        patched_files: dict[str, Any],
        reproduction_command: str = "",
        regression_command: str = "",
    ) -> dict[str, Any]:
        """Grade a claim on the evidence ladder by running a before/after
        reproduction (and optional regression) in the sandboxed verifier."""
        if not isinstance(original_files, dict) or not isinstance(patched_files, dict):
            raise ValueError("'original' and 'patched' file maps are required")
        original = {
            path: text
            for path, text in original_files.items()
            if isinstance(path, str) and isinstance(text, str)
        }
        patched = {
            path: text
            for path, text in patched_files.items()
            if isinstance(path, str) and isinstance(text, str)
        }
        total = sum(
            len(text.encode("utf-8")) for text in list(original.values()) + list(patched.values())
        )
        if total > self.settings.max_diff_bytes * 10:
            raise ValueError("proof payload exceeds the maximum analysable size")
        result = ProofRunner(self._proof_verifier).prove(
            original,
            patched,
            str(reproduction_command or ""),
            str(regression_command or ""),
        )
        metrics.inc("proof_runs_total")
        return result

    def reload_skills(self) -> list:
        if self.llm_config:
            active = self.store.get_active_skill_version("llm-review")
            self.registry.register(
                "llm-review",
                self._build_llm_reviewer(active["prompt"] if active else ""),
                "1.0.0",
                "Context-aware AI code review via %s" % self.llm_config["provider"],
            )
        skills = self.registry.reload()
        self.reviewer = MultiAgentCoordinator(self.registry.reviewers(), store=self.store)
        self.harness = ReviewHarness(
            self.store,
            self.reviewer,
            self.settings.max_steps,
            self.settings.timeout_seconds,
            observability=self.observability,
        )
        return skills

    def _validate_review(self, repository: str, diff: str) -> None:
        if not repository or len(repository) > 250:
            raise ValueError("repository is required and must be at most 250 characters")
        size = len(diff.encode("utf-8"))
        if size == 0:
            raise ValueError("diff is required")
        if size > self.settings.max_diff_bytes:
            raise ValueError("diff exceeds maximum size of %d bytes" % self.settings.max_diff_bytes)

    def _create_task(
        self,
        repository: str,
        diff: str,
        pull_request: int | None,
        source: str,
        tenant_id: str = "default",
    ) -> str:
        task_id = str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        self.store.create(
            task_id,
            repository,
            pull_request,
            {
                "source": source,
                "diff_bytes": len(encoded),
                "diff_sha256": hashlib.sha256(encoded).hexdigest(),
                "release_lane": assignment["lane"],
                "shadow": assignment["shadow"],
            },
            tenant_id,
        )
        self.store.save_task_payload(task_id, diff)
        return task_id

    def _create_deferred_task(
        self,
        repository: str,
        pull_request: int | None,
        source: str,
        tenant_id: str,
        payload: dict[str, Any],
    ) -> str:
        task_id = str(uuid.uuid4())
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        self.store.create(
            task_id,
            repository,
            pull_request,
            {
                "source": source,
                "diff_pending": True,
                "release_lane": assignment["lane"],
                "shadow": assignment["shadow"],
                **payload,
            },
            tenant_id,
        )
        return task_id

    def create_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        self._validate_review(repository, diff)
        self._authorize_repository(tenant_id, repository)
        task_id = self._create_task(repository, diff, pull_request, source, tenant_id)
        try:
            with (
                self.observability.span(
                    "review",
                    task_id,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    repository=repository,
                ),
                metrics.timer("review_duration"),
            ):
                report = self._run_review(task_id, repository, pull_request, diff, tenant_id)
            self._run_shadow(task_id, tenant_id, diff, report)
            metrics.inc("reviews_total")
            lane = (self.store.get(task_id, tenant_id).get("input") or {}).get(
                "release_lane", "stable"
            )
            self.releases.observe(tenant_id, "llm-review", False, lane)
            return {"task_id": task_id, "state": "SUCCESS", "report": report.to_dict()}
        except Exception:
            task = self.store.get(task_id, tenant_id) or {}
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def enqueue_review(
        self,
        repository: str,
        diff: str,
        pull_request: int | None = None,
        source: str = "api",
        github_issue_url: str = "",
        installation_id: int | None = None,
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        self._validate_review(repository, diff)
        self._authorize_repository(tenant_id, repository)
        task_id = self._create_task(repository, diff, pull_request, source, tenant_id)
        self.queue.submit(
            {
                "task_id": task_id,
                "repository": repository,
                "pull_request": pull_request,
                "github_issue_url": github_issue_url,
                "installation_id": installation_id,
                "tenant_id": tenant_id,
            },
            message_id=task_id,
        )
        metrics.inc("reviews_enqueued_total")
        return {"task_id": task_id, "state": "PENDING", "queue": self.queue.backend}

    def _process_queued(self, payload: dict[str, Any]) -> None:
        task_id = payload["task_id"]
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        tenant_id = payload.get("tenant_id") or task.get("tenant_id") or "default"
        diff = self.store.get_task_payload(task_id)
        if diff is None and payload.get("diff_url"):
            client = (
                self.github_client_for_installation(payload.get("installation_id"))
                if payload.get("installation_id")
                else self.github
            )
            client.ensure_repository_access(payload["repository"])
            diff = client.fetch_diff(
                payload["diff_url"], max_bytes=self.settings.max_diff_bytes + 4096
            )
            self._validate_review(payload["repository"], diff)
            encoded = diff.encode("utf-8")
            self.store.save_task_payload(task_id, diff)
            self.store.update_task_input(
                task_id,
                {
                    "diff_pending": False,
                    "diff_bytes": len(encoded),
                    "diff_sha256": hashlib.sha256(encoded).hexdigest(),
                },
            )
        if diff is None:
            raise PermanentTaskError("task payload no longer exists")
        try:
            with (
                self.observability.span(
                    "review.async",
                    task_id,
                    task_id=task_id,
                    tenant_id=tenant_id,
                ),
                metrics.timer("review_duration"),
            ):
                report = self._run_review(
                    task_id,
                    payload["repository"],
                    payload.get("pull_request"),
                    diff,
                    tenant_id,
                )
            self._run_shadow(task_id, tenant_id, diff, report)
            metrics.inc("reviews_total")
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", False, lane)
            continuity = self._record_session_turn(payload, report)
            if payload.get("github_issue_url") and self.settings.auto_post_review:
                client = self.github_client_for_installation(payload.get("installation_id"))
                body = to_markdown(report.to_dict())
                if continuity:
                    body += "\n\n" + continuity
                marker = (
                    "<!-- evoagent-session:%s -->" % payload["session_id"]
                    if payload.get("session_id")
                    else "<!-- evoagent-review:%s -->" % task_id
                )
                client.upsert_comment(payload["github_issue_url"], body, marker)
        except Exception:
            metrics.inc("reviews_failed_total")
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def _on_dead_letter(self, payload: dict[str, Any], error: str) -> None:
        task_id = payload.get("task_id", "")
        tenant_id = payload.get("tenant_id", "default")
        task = self.store.get(task_id, tenant_id) if task_id else None
        if task and task.get("state") not in {
            TaskState.SUCCESS.value,
            TaskState.FAILED.value,
            TaskState.CANCELLED.value,
        }:
            step = max([int(item.get("step", 0)) for item in task.get("trace", [])] or [0]) + 1
            self.store.fail(
                task_id,
                error,
                TraceEvent(
                    step,
                    TaskState.FAILED,
                    "Task entered the dead-letter queue: %s" % error,
                    utc_now(),
                ),
            )
        self.store.create_alert(
            tenant_id,
            "dlq:%s" % (task_id or "unknown"),
            "critical",
            "Task %s entered the dead-letter queue: %s" % (task_id, error),
        )
        metrics.inc("dead_letters_total")

    def handle_github_pull_request(
        self,
        payload: dict[str, Any],
        delivery_id: str,
        payload_sha256: str,
        tenant_id: str = "",
    ) -> dict[str, Any]:
        installation_id = (payload.get("installation") or {}).get("id")
        tenant_id = (
            tenant_id
            or (self.store.installation_tenant(installation_id) if installation_id else None)
            or self.settings.default_tenant_id
        )
        if not self.store.claim_webhook(delivery_id, tenant_id, "pull_request", payload_sha256):
            existing = self.store.get_webhook(delivery_id) or {}
            return {
                "duplicate": True,
                "task_id": existing.get("task_id"),
                "state": "PENDING" if existing.get("task_id") else "ACCEPTED",
            }
        action = payload.get("action")
        if action not in {"opened", "reopened", "synchronize"}:
            self.store.complete_webhook(delivery_id, None)
            return {"ignored": True, "reason": "unsupported pull_request action: %s" % action}
        pull = payload.get("pull_request") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        number = payload.get("number")
        diff_url = pull.get("diff_url")
        if not repository or not isinstance(number, int) or not diff_url:
            raise ValueError("invalid GitHub pull_request payload")
        self._authorize_repository(tenant_id, repository)
        head_sha = (pull.get("head") or {}).get("sha")
        session = self.store.start_session_turn(tenant_id, repository, number, head_sha, action)
        task_id = self._create_deferred_task(
            repository,
            number,
            "github-webhook",
            tenant_id,
            {
                "diff_url": diff_url,
                "session_id": session["session_id"],
                "turn_id": session["turn_id"],
                "head_sha": head_sha,
                "trigger": action,
            },
        )
        self.queue.submit(
            {
                "task_id": task_id,
                "repository": repository,
                "pull_request": number,
                "github_issue_url": pull.get("issue_url", ""),
                "installation_id": installation_id,
                "tenant_id": tenant_id,
                "diff_url": diff_url,
                "session_id": session["session_id"],
                "turn_id": session["turn_id"],
                "head_sha": head_sha,
            },
            message_id=task_id,
        )
        metrics.inc("reviews_enqueued_total")
        result: dict[str, object] = {
            "task_id": task_id,
            "state": "PENDING",
            "queue": self.queue.backend,
            "session_id": session["session_id"],
            "turn": session["sequence"],
        }
        self.store.complete_webhook(delivery_id, task_id)
        result["will_post_to_github"] = self.settings.auto_post_review
        return result

    def github_client_for_installation(self, installation_id: int | None = None) -> GitHubClient:
        if installation_id is None:
            return self.github
        if not self.settings.github_app_id or not self.settings.github_private_key_path:
            raise ValueError("GitHub App credentials are not configured")
        token = GitHubAppAuthenticator(
            self.settings.github_app_id, self.settings.github_private_key_path
        ).installation_token(installation_id)
        return GitHubClient(token)

    def create_fix(
        self,
        task_id: str,
        installation_id: int | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task or not task.get("report"):
            raise ValueError("completed task not found")
        if task.get("pull_request") is None:
            raise ValueError("fix commits require a GitHub pull request task")
        actual_tenant = task.get("tenant_id") or tenant_id or "default"
        if not self.store.repository_allowed(actual_tenant, task["repository"], True):
            raise PermissionError("automatic repair is not enabled for this repository")
        result = self.fixer.create_fix_commits(
            self.github_client_for_installation(installation_id),
            task["repository"],
            task["pull_request"],
            task["report"],
        )
        metrics.inc("fix_runs_total")
        return result

    def record_feedback(
        self,
        task_id: str,
        category: str,
        finding: dict | None,
        note: str,
        tenant_id: str | None = None,
    ) -> dict:
        if not self.store.get(task_id, tenant_id):
            raise ValueError("task not found")
        if category not in {"false_positive", "missed_issue", "bad_fix", "accepted"}:
            raise ValueError("unsupported feedback category")
        self.store.record_failure_case(task_id, category, {"finding": finding, "note": note[:2000]})
        metrics.inc("feedback_total")
        return {"recorded": True, "category": category}

    def resume_task(self, task_id: str, tenant_id: str | None = None) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task["state"] == "SUCCESS":
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        diff = self.store.get_task_payload(task_id)
        if diff is None:
            raise ValueError("task payload is no longer available")
        self.queue.submit(
            {
                "task_id": task_id,
                "repository": task["repository"],
                "pull_request": task.get("pull_request"),
                "tenant_id": task.get("tenant_id", "default"),
            },
            message_id=task_id,
        )
        return {"task_id": task_id, "state": "PENDING", "resumed": True}

    def cancel_task(self, task_id: str, tenant_id: str | None = None) -> bool:
        return self.store.request_cancel(task_id, tenant_id)

    def _authorize_repository(self, tenant_id: str, repository: str) -> None:
        if not self.store.repository_allowed(tenant_id, repository):
            raise PermissionError("repository is not authorized for this tenant")
