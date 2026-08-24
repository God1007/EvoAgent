"""Session continuity and source-impact application use cases."""

from __future__ import annotations

from typing import Any

from ..codegraph import build_graph
from ..errors import ClientInputError, ResourceNotFoundError, StateConflictError
from ..metrics import metrics
from ..ports import SessionApplicationStorePort
from ..repository import canonical_repository
from ..session import classify_findings, continuity_summary, open_snapshot


class SessionUseCases:
    def __init__(self, store: SessionApplicationStorePort, max_diff_bytes: int):
        self.store = store
        self.max_diff_bytes = max_diff_bytes

    def record_review_turn(self, payload: dict[str, Any], report: Any) -> str:
        """Persist one review turn and return its markdown continuity footer."""
        session_id = payload.get("session_id")
        turn_id = payload.get("turn_id")
        if not session_id or not turn_id:
            return ""
        repository = payload["repository"]
        previous = self.store.previous_open_snapshot(session_id, turn_id)
        classified = classify_findings(repository, previous, list(report.findings))
        summary = continuity_summary(classified)
        snapshots = open_snapshot(repository, classified)
        completed = self.store.complete_session_turn(
            session_id,
            turn_id,
            payload.get("task_id"),
            snapshots,
            summary,
            payload.get("head_sha"),
        )
        if completed:
            metrics.inc("session_turns_total")
        return "" if not previous else self.continuity_note(summary)

    @staticmethod
    def continuity_note(summary: dict[str, int]) -> str:
        return "> **会话连续性** — 新增 %d · 仍存在 %d · 已修复 %d · 移动 %d（当前未解决 %d）" % (
            summary["new"],
            summary["still_open"],
            summary["resolved"],
            summary["moved"],
            summary["open"],
        )

    def get_timeline(self, session_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        return self.store.get_session_timeline(session_id, tenant_id)

    def get_for_pull_request(
        self, repository: str, pull_request: int, tenant_id: str = "default"
    ) -> dict[str, Any] | None:
        repository = canonical_repository(repository)
        session = self.store.get_session(tenant_id, repository, pull_request)
        if not session:
            return None
        return self.store.get_session_timeline(session["id"], tenant_id)

    def provide_input(
        self,
        session_id: str,
        message: str,
        tenant_id: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip():
            raise ClientInputError("session input must be a non-empty string")
        timeline = self.store.get_session_timeline(session_id, tenant_id)
        if timeline is None:
            raise ResourceNotFoundError("session not found")
        if timeline.get("status") != "input-required" or not self.store.resolve_session_input(
            session_id,
            tenant_id or str(timeline.get("tenant_id") or "default"),
            actor,
        ):
            raise StateConflictError("session is not waiting for input")
        return {"session_id": session_id, "status": "open"}

    def analyze_impact(self, sources: dict[str, Any], changed_paths: list[Any]) -> dict[str, Any]:
        if not isinstance(sources, dict) or not isinstance(changed_paths, list):
            raise ClientInputError("'files' object and 'changed' list are required")
        if any(
            not isinstance(path, str) or not isinstance(value, str)
            for path, value in sources.items()
        ):
            raise ClientInputError("'files' must map string paths to string contents")
        if any(not isinstance(path, str) for path in changed_paths):
            raise ClientInputError("'changed' must contain only string paths")
        if len(sources) > 5000 or len(changed_paths) > 5000:
            raise ClientInputError("too many files or changed paths to analyse in one request")
        try:
            total = sum(
                len(path.encode("utf-8")) + len(value.encode("utf-8"))
                for path, value in sources.items()
            ) + sum(len(path.encode("utf-8")) for path in changed_paths)
        except UnicodeEncodeError:
            raise ClientInputError("source paths and contents must be valid UTF-8") from None
        if total > self.max_diff_bytes * 10:
            raise ClientInputError("source payload exceeds the maximum analysable size")
        graph = build_graph(sources)
        return graph.impact_of(changed_paths)
