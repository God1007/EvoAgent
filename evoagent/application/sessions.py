"""Session continuity and source-impact application use cases."""

from __future__ import annotations

from typing import Any

from ..codegraph import build_graph
from ..metrics import metrics
from ..ports import SessionApplicationStorePort
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
        self.store.complete_session_turn(
            session_id,
            turn_id,
            payload.get("task_id"),
            snapshots,
            summary,
            payload.get("head_sha"),
        )
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
        session = self.store.get_session(tenant_id, repository, pull_request)
        if not session:
            return None
        return self.store.get_session_timeline(session["id"], tenant_id)

    def provide_input(
        self, session_id: str, message: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
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
        if not isinstance(sources, dict) or not isinstance(changed_paths, list):
            raise ValueError("'files' object and 'changed' list are required")
        normalized = {
            path: value
            for path, value in sources.items()
            if isinstance(path, str) and isinstance(value, str)
        }
        if len(normalized) > 5000:
            raise ValueError("too many files to analyse in a single request")
        total = sum(len(value.encode("utf-8")) for value in normalized.values())
        if total > self.max_diff_bytes * 10:
            raise ValueError("source payload exceeds the maximum analysable size")
        graph = build_graph(normalized)
        return graph.impact_of([path for path in changed_paths if isinstance(path, str)])
