"""Checkpointed review state machine.

Checkpoints are owned by the application store and survive worker restarts.
"""

import math
import threading
import time
from typing import Any, TypedDict, cast

from .diff_parser import ParsedDiff, parse_unified_diff
from .errors import safe_exception_summary
from .metrics import metrics
from .models import Finding, ReviewReport, Severity, TaskState, TraceEvent
from .ports import ReviewExecutionStorePort
from .reviewer import Reviewer
from .time_utils import utc_now

ALLOWED = {
    TaskState.PENDING: {TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PLANNING: {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.REVIEWING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.REVIEWING: {TaskState.SUCCESS, TaskState.FAILED, TaskState.CANCELLED},
}


class RuntimeState(TypedDict, total=False):
    task_id: str
    repository: str
    pull_request: int | None
    diff: str
    parsed: dict[str, Any]
    findings: list
    report: dict[str, Any]


class BudgetExceeded(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    pass


class ReviewHarness:
    node_order = ("planning", "executing", "reviewing")

    def __init__(
        self,
        store: ReviewExecutionStorePort,
        reviewer: Reviewer,
        max_steps: int = 8,
        timeout_seconds: int = 120,
        node_retries: int = 2,
        observability=None,
    ):
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("review harness max_steps must be a positive integer")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("review harness timeout must be positive and finite")
        if isinstance(node_retries, bool) or not isinstance(node_retries, int) or node_retries < 0:
            raise ValueError("review harness node_retries must be a non-negative integer")
        self.store = store
        self.reviewer = reviewer
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.node_retries = node_retries
        self.observability = observability
        self.name = "durable-state-machine"
        self._ctx = threading.local()

    def run(
        self,
        task_id: str,
        repository: str,
        pull_request: int | None,
        diff: str,
        admission_generation: int | None = None,
    ) -> ReviewReport:
        task = self.store.get(task_id)
        if task and task.get("state") == TaskState.SUCCESS.value and task.get("report"):
            return self._report_from_dict(task["report"])
        state: RuntimeState = {
            "task_id": task_id,
            "repository": repository,
            "pull_request": pull_request,
            "diff": diff,
        }
        self._ctx.started = time.monotonic()
        self._ctx.step = max([item["step"] for item in (task or {}).get("trace", [])] or [0])
        self._ctx.task_id = task_id
        self._ctx.admission_generation = admission_generation
        checkpoints = self.store.load_checkpoints(task_id)
        self._ctx.state = TaskState.PENDING
        if checkpoints.get("planning", {}).get("status") == "completed":
            self._ctx.state = TaskState.PLANNING
        if checkpoints.get("executing", {}).get("status") == "completed":
            self._ctx.state = TaskState.EXECUTING
        if checkpoints.get("reviewing", {}).get("status") == "completed":
            self._ctx.state = TaskState.REVIEWING
        try:
            result: dict[str, Any] = dict(state)
            for node in (self._planning, self._executing, self._reviewing):
                result.update(node(cast(RuntimeState, result)))
            report = self._report_from_dict(result["report"])
            self._ctx.step += 1
            if (
                self.store.succeed(
                    task_id,
                    report,
                    TraceEvent(self._ctx.step, TaskState.SUCCESS, "Review completed", utc_now()),
                    admission_generation,
                )
                is False
            ):
                raise TaskCancelled("Task was cancelled")
            return report
        except TaskCancelled:
            self._ctx.step += 1
            self.store.cancel(
                task_id,
                TraceEvent(self._ctx.step, TaskState.CANCELLED, "Task was cancelled", utc_now()),
                admission_generation,
            )
            raise
        except Exception as exc:
            self._ctx.step += 1
            summary = safe_exception_summary(exc, "review execution failed")
            if (
                self.store.fail(
                    task_id,
                    summary,
                    TraceEvent(self._ctx.step, TaskState.FAILED, summary, utc_now()),
                    admission_generation,
                )
                is False
            ):
                self.store.cancel(
                    task_id,
                    TraceEvent(
                        self._ctx.step,
                        TaskState.CANCELLED,
                        "Task was cancelled",
                        utc_now(),
                    ),
                    admission_generation,
                )
                raise TaskCancelled("Task was cancelled") from exc
            try:
                self.store.record_failure_case(
                    task_id,
                    "execution_error",
                    {"error": summary},
                    admission_generation,
                )
            except Exception:
                metrics.inc("failure_case_persistence_failures_total")
            raise

    def _planning(self, state: RuntimeState) -> dict[str, Any]:
        def work():
            parsed = parse_unified_diff(state["diff"])
            if not parsed.files and not parsed.added_lines:
                raise ValueError("diff does not contain a valid unified diff with added lines")
            self._transition(TaskState.PLANNING, "Input accepted; preparing review plan")
            return {"parsed": parsed.to_dict()}

        return self._run_node(state["task_id"], "planning", work)

    def _executing(self, state: RuntimeState) -> dict[str, Any]:
        def work():
            parsed = ParsedDiff.from_dict(state["parsed"])
            self._transition(TaskState.EXECUTING, "Reviewing %d changed files" % len(parsed.files))
            contextual = getattr(self.reviewer, "review_with_context", None)
            findings = (
                contextual(
                    state["task_id"],
                    state["diff"],
                    parsed,
                    getattr(self._ctx, "admission_generation", None),
                )
                if contextual
                else self.reviewer.review(state["diff"], parsed)
            )
            return {"findings": [item.to_dict() for item in findings]}

        return self._run_node(state["task_id"], "executing", work)

    def _reviewing(self, state: RuntimeState) -> dict[str, Any]:
        def work():
            parsed = ParsedDiff.from_dict(state["parsed"])
            findings = [self._finding_from_dict(item) for item in state["findings"]]
            self._transition(
                TaskState.REVIEWING, "Validating and ranking %d findings" % len(findings)
            )
            risk = self._risk(findings)
            report = ReviewReport(
                repository=state["repository"],
                pull_request=state.get("pull_request"),
                summary=self._summary(findings, len(parsed.files), risk),
                risk=risk,
                findings=findings,
                files_reviewed=parsed.files,
                reviewer=self.reviewer.name,
            )
            return {"report": report.to_dict()}

        return self._run_node(state["task_id"], "reviewing", work)

    def _run_node(self, task_id: str, node: str, callback) -> dict[str, Any]:
        last_error: Exception | None = None
        existing = self.store.load_checkpoints(task_id).get(node, {})
        if existing.get("status") == "completed":
            return existing["state"]
        start_attempt = int(existing.get("attempt", 0))
        for offset in range(1, self.node_retries + 2):
            attempt = start_attempt + offset
            self._check_budget(task_id)
            try:
                if self.observability:
                    with self.observability.span(
                        "review.%s" % node,
                        task_id,
                        task_id=task_id,
                        node=node,
                        attempt=attempt,
                    ):
                        output = callback()
                else:
                    output = callback()
                self._check_budget(task_id)
                if (
                    self.store.save_checkpoint(
                        task_id,
                        node,
                        output,
                        "completed",
                        attempt,
                        generation=getattr(self._ctx, "admission_generation", None),
                    )
                    is False
                ):
                    raise TaskCancelled("Task was cancelled")
                completed = self._completed(task_id, node)
                return completed if completed is not None else output
            except (TaskCancelled, BudgetExceeded):
                raise
            except ValueError:
                completed = self._completed(task_id, node)
                if completed is not None:
                    return completed
                raise
            except Exception as exc:
                last_error = exc
                if (
                    self.store.save_checkpoint(
                        task_id,
                        node,
                        {},
                        "failed",
                        attempt,
                        safe_exception_summary(exc, "review node failed"),
                        getattr(self._ctx, "admission_generation", None),
                    )
                    is False
                ):
                    raise TaskCancelled("Task was cancelled") from exc
                completed = self._completed(task_id, node)
                if completed is not None:
                    return completed
        if last_error is None:
            raise RuntimeError("review node failed without an exception")
        raise last_error

    def _completed(self, task_id: str, node: str) -> dict[str, Any] | None:
        checkpoint = self.store.load_checkpoints(task_id).get(node)
        if checkpoint and checkpoint["status"] == "completed":
            return checkpoint["state"]
        return None

    def _transition(self, target: TaskState, message: str) -> None:
        self._check_budget("")
        if target == self._ctx.state:
            return
        if target not in ALLOWED.get(self._ctx.state, set()):
            raise RuntimeError(
                "invalid state transition: %s -> %s" % (self._ctx.state.value, target.value)
            )
        self._ctx.step += 1
        self._ctx.state = target
        if (
            self.store.transition(
                self._ctx.task_id,
                TraceEvent(self._ctx.step, target, message, utc_now()),
                getattr(self._ctx, "admission_generation", None),
            )
            is False
        ):
            raise TaskCancelled("Task was cancelled")

    def _check_budget(self, task_id: str) -> None:
        effective_task_id = task_id or getattr(self._ctx, "task_id", "")
        if effective_task_id and self.store.is_cancelled(effective_task_id):
            raise TaskCancelled("Task was cancelled")
        if (
            self._ctx.step >= self.max_steps
            or time.monotonic() - self._ctx.started > self.timeout_seconds
        ):
            raise BudgetExceeded("task execution budget exceeded")

    @staticmethod
    def _finding_from_dict(value: dict[str, Any]) -> Finding:
        return Finding.from_dict(value)

    @classmethod
    def _report_from_dict(cls, value: dict[str, Any]) -> ReviewReport:
        return ReviewReport.from_dict(value)

    @staticmethod
    def _risk(findings) -> str:
        severities = {item.severity for item in findings}
        if Severity.CRITICAL in severities or Severity.HIGH in severities:
            return "high"
        if Severity.MEDIUM in severities:
            return "medium"
        return "low"

    @staticmethod
    def _summary(findings, file_count: int, risk: str) -> str:
        if not findings:
            return (
                "Reviewed %d file(s); no actionable issue was detected in added lines." % file_count
            )
        return "Reviewed %d file(s); found %d actionable issue(s). Overall risk: %s." % (
            file_count,
            len(findings),
            risk,
        )
