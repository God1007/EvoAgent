"""Bounded operational-history retention with explicit business-state anchors."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .metrics import metrics
from .ports import ServiceStorePort


@dataclass(frozen=True)
class RetentionOptions:
    retention_days: int = 0
    interval_seconds: int = 3600
    batch_size: int = 1000
    max_batches_per_run: int = 10


class RetentionManager:
    """Periodically prune only history no longer needed by live state.

    Store adapters own the transactional safety predicates. This coordinator
    bounds work per pass, emits fixed-cardinality telemetry, and participates in
    service shutdown before the Store capability is closed.
    """

    def __init__(
        self,
        store: ServiceStorePort,
        options: RetentionOptions,
        *,
        autostart: bool = True,
    ):
        self.store = store
        self.options = options
        self.enabled = options.retention_days > 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_success_timestamp = 0.0
        self._last_failure_timestamp = 0.0
        self._last_error_type = ""
        self._last_counts = self._empty_counts()
        self._thread: threading.Thread | None = None
        if self.enabled and autostart:
            self._thread = threading.Thread(
                target=self._run,
                name="evoagent-retention",
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {"trace_events": 0, "session_turns": 0, "session_findings": 0}

    def run_once(self, now: datetime | None = None) -> dict[str, int]:
        if not self.enabled:
            return self._empty_counts()
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = (current - timedelta(days=self.options.retention_days)).isoformat()
        pruned_at = current.isoformat()
        totals = self._empty_counts()
        for _ in range(max(1, self.options.max_batches_per_run)):
            result = self.store.prune_operational_history(
                cutoff,
                cutoff,
                self.options.batch_size,
                pruned_at,
            )
            batch = {
                key: max(0, int(result.get(key, 0)))
                for key in ("trace_events", "session_turns", "session_findings")
            }
            for key, value in batch.items():
                totals[key] += value
            if (
                batch["trace_events"] < self.options.batch_size
                and batch["session_turns"] < self.options.batch_size
            ):
                break
        metrics.inc("retention_runs_total")
        metrics.inc("retention_trace_events_pruned_total", totals["trace_events"])
        metrics.inc("retention_session_turns_pruned_total", totals["session_turns"])
        metrics.inc("retention_session_findings_pruned_total", totals["session_findings"])
        with self._lock:
            self._last_success_timestamp = current.timestamp()
            self._last_error_type = ""
            self._last_counts = totals
        return dict(totals)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._run_safely_once()
            if self._stop.wait(self.options.interval_seconds):
                return

    def _run_safely_once(self) -> bool:
        try:
            self.run_once()
            return True
        except Exception as exc:
            metrics.inc("retention_failures_total")
            error_type = "%s.%s" % (type(exc).__module__, type(exc).__qualname__)
            with self._lock:
                self._last_failure_timestamp = time.time()
                self._last_error_type = error_type[:200]
            return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            return {
                "enabled": self.enabled,
                "running": bool(thread and thread.is_alive()),
                "retention_days": self.options.retention_days,
                "interval_seconds": self.options.interval_seconds,
                "last_success_timestamp": self._last_success_timestamp,
                "last_failure_timestamp": self._last_failure_timestamp,
                "last_error_type": self._last_error_type or None,
                "last_pruned": dict(self._last_counts),
            }

    def last_success_timestamp(self) -> float:
        with self._lock:
            return self._last_success_timestamp

    def close(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(max(0.0, timeout))
        return not thread.is_alive()
