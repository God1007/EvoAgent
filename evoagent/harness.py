"""Checkpointed review graph.

LangGraph is used when installed. The same node functions have a small local
executor so a developer checkout remains usable without optional dependencies.
Checkpoints are owned by the application store and therefore work with either
executor and survive worker restarts.
"""
import threading
import time
from typing import Any, Dict, Optional, TypedDict

from .diff_parser import ParsedDiff, parse_unified_diff
from .models import ChangedLine, Finding, ReviewReport, Severity, TaskState, TraceEvent
from .reviewer import Reviewer
from .store import TaskStore, utc_now


ALLOWED = {
    TaskState.PENDING: {TaskState.PLANNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PLANNING: {TaskState.EXECUTING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.EXECUTING: {TaskState.REVIEWING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.REVIEWING: {TaskState.SUCCESS, TaskState.FAILED, TaskState.CANCELLED},
}


class RuntimeState(TypedDict, total=False):
    task_id: str
    repository: str
    pull_request: Optional[int]
    diff: str
    parsed: Dict[str, Any]
    findings: list
    report: Dict[str, Any]


class BudgetExceeded(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    pass


class ReviewHarness:
    node_order = ("planning", "executing", "reviewing")

    def __init__(
        self, store: TaskStore, reviewer: Reviewer, max_steps: int = 8,
        timeout_seconds: int = 120, node_retries: int = 2, observability=None,
    ):
        self.store = store
        self.reviewer = reviewer
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.node_retries = node_retries
        self.observability = observability
        self.name = "langgraph"
        self._ctx = threading.local()
        self.graph = self._build_graph()

    def _build_graph(self):
        try:
            from langgraph.graph import END, START, StateGraph
            builder = StateGraph(RuntimeState)
            builder.add_node("planning", self._planning)
            builder.add_node("executing", self._executing)
            builder.add_node("reviewing", self._reviewing)
            builder.add_edge(START, "planning")
            builder.add_edge("planning", "executing")
            builder.add_edge("executing", "reviewing")
            builder.add_edge("reviewing", END)
            return builder.compile()
        except ImportError:
            self.name = "durable-graph-fallback"
            return None

    def run(
        self, task_id: str, repository: str, pull_request: Optional[int], diff: str,
    ) -> ReviewReport:
        task = self.store.get(task_id)
        if task and task.get("state") == TaskState.SUCCESS.value and task.get("report"):
            return self._report_from_dict(task["report"])
        state: RuntimeState = {
            "task_id": task_id, "repository": repository,
            "pull_request": pull_request, "diff": diff,
        }
        self._ctx.started = time.monotonic()
        self._ctx.step = max([item["step"] for item in (task or {}).get("trace", [])] or [0])
        self._ctx.task_id = task_id
        checkpoints = self.store.load_checkpoints(task_id)
        self._ctx.state = TaskState.PENDING
        if checkpoints.get("planning", {}).get("status") == "completed":
            self._ctx.state = TaskState.PLANNING
        if checkpoints.get("executing", {}).get("status") == "completed":
            self._ctx.state = TaskState.EXECUTING
        if checkpoints.get("reviewing", {}).get("status") == "completed":
            self._ctx.state = TaskState.REVIEWING
        try:
            if self.graph is not None:
                result = self.graph.invoke(state)
            else:
                result = state
                for node in (self._planning, self._executing, self._reviewing):
                    result.update(node(result))
            report = self._report_from_dict(result["report"])
            self._ctx.step += 1
            self.store.succeed(
                task_id, report,
                TraceEvent(self._ctx.step, TaskState.SUCCESS, "Review completed", utc_now()),
            )
            return report
        except TaskCancelled as exc:
            self._ctx.step += 1
            self.store.cancel(
                task_id, TraceEvent(self._ctx.step, TaskState.CANCELLED, str(exc), utc_now())
            )
            raise
        except Exception as exc:
            self._ctx.step += 1
            self.store.fail(
                task_id, str(exc),
                TraceEvent(self._ctx.step, TaskState.FAILED, "Review failed: %s" % exc, utc_now()),
            )
            try:
                self.store.record_failure_case(
                    task_id, "execution_error", {"error": str(exc)[:1000]}
                )
            except Exception:
                pass
            raise

    def resume(
        self, task_id: str, repository: str, pull_request: Optional[int], diff: str,
    ) -> ReviewReport:
        return self.run(task_id, repository, pull_request, diff)

    def _planning(self, state: RuntimeState) -> Dict[str, Any]:
        cached = self._completed(state["task_id"], "planning")
        if cached is not None:
            return cached

        def work():
            parsed = parse_unified_diff(state["diff"])
            if not parsed.files and not parsed.added_lines:
                raise ValueError("diff does not contain a valid unified diff with added lines")
            self._transition(TaskState.PLANNING, "Input accepted; preparing review plan")
            return {"parsed": self._serialize_parsed(parsed)}

        return self._run_node(state["task_id"], "planning", work)

    def _executing(self, state: RuntimeState) -> Dict[str, Any]:
        cached = self._completed(state["task_id"], "executing")
        if cached is not None:
            return cached

        def work():
            parsed = self._deserialize_parsed(state["parsed"])
            self._transition(
                TaskState.EXECUTING, "Reviewing %d changed files" % len(parsed.files)
            )
            contextual = getattr(self.reviewer, "review_with_context", None)
            findings = (
                contextual(state["task_id"], state["diff"], parsed)
                if contextual else self.reviewer.review(state["diff"], parsed)
            )
            return {"findings": [item.to_dict() for item in findings]}

        return self._run_node(state["task_id"], "executing", work)

    def _reviewing(self, state: RuntimeState) -> Dict[str, Any]:
        cached = self._completed(state["task_id"], "reviewing")
        if cached is not None:
            return cached

        def work():
            parsed = self._deserialize_parsed(state["parsed"])
            findings = [self._finding_from_dict(item) for item in state["findings"]]
            self._transition(
                TaskState.REVIEWING, "Validating and ranking %d findings" % len(findings)
            )
            risk = self._risk(findings)
            report = ReviewReport(
                repository=state["repository"], pull_request=state.get("pull_request"),
                summary=self._summary(findings, len(parsed.files), risk), risk=risk,
                findings=findings, files_reviewed=parsed.files, reviewer=self.reviewer.name,
            )
            return {"report": report.to_dict()}

        return self._run_node(state["task_id"], "reviewing", work)

    def _run_node(self, task_id: str, node: str, callback) -> Dict[str, Any]:
        last_error = None
        existing = self.store.load_checkpoints(task_id).get(node, {})
        start_attempt = int(existing.get("attempt", 0))
        for offset in range(1, self.node_retries + 2):
            attempt = start_attempt + offset
            self._check_budget(task_id)
            try:
                if self.observability:
                    with self.observability.span(
                        "review.%s" % node, task_id, task_id=task_id,
                        node=node, attempt=attempt,
                    ):
                        output = callback()
                else:
                    output = callback()
                self.store.save_checkpoint(task_id, node, output, "completed", attempt)
                return output
            except (ValueError, TaskCancelled, BudgetExceeded):
                raise
            except Exception as exc:
                last_error = exc
                self.store.save_checkpoint(
                    task_id, node, {}, "failed", attempt, str(exc)
                )
        raise last_error

    def _completed(self, task_id: str, node: str) -> Optional[Dict[str, Any]]:
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
        self.store.transition(
            self._ctx.task_id,
            TraceEvent(self._ctx.step, target, message, utc_now()),
        )

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
    def _serialize_parsed(parsed: ParsedDiff) -> Dict[str, Any]:
        return {
            "files": parsed.files,
            "added_lines": [
                {"path": item.path, "line": item.line, "content": item.content}
                for item in parsed.added_lines
            ],
        }

    @staticmethod
    def _deserialize_parsed(value: Dict[str, Any]) -> ParsedDiff:
        return ParsedDiff(
            list(value["files"]), [ChangedLine(**item) for item in value["added_lines"]]
        )

    @staticmethod
    def _finding_from_dict(value: Dict[str, Any]) -> Finding:
        item = dict(value)
        item["severity"] = Severity(item["severity"])
        return Finding(**item)

    @classmethod
    def _report_from_dict(cls, value: Dict[str, Any]) -> ReviewReport:
        return ReviewReport(
            repository=value["repository"], pull_request=value.get("pull_request"),
            summary=value["summary"], risk=value["risk"],
            findings=[cls._finding_from_dict(item) for item in value.get("findings", [])],
            files_reviewed=list(value.get("files_reviewed", [])),
            reviewer=value.get("reviewer", "unknown"),
        )

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
            return "Reviewed %d file(s); no actionable issue was detected in added lines." % file_count
        return "Reviewed %d file(s); found %d actionable issue(s). Overall risk: %s." % (
            file_count, len(findings), risk,
        )
