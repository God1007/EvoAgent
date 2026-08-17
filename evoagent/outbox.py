"""Transactional outbox dispatcher bridging the Store and TaskQueue ports."""

from __future__ import annotations

import os
import socket
import threading
import uuid
from typing import Any

from .errors import safe_exception_summary
from .metrics import metrics
from .ports import OutboxStorePort, TaskQueuePort


class OutboxDispatcher:
    """Lease and publish committed outbox rows with idempotent message keys."""

    def __init__(
        self,
        store: OutboxStorePort,
        queue: TaskQueuePort,
        poll_seconds: float = 0.25,
        batch_size: int = 50,
        lease_seconds: float = 30.0,
        max_attempts: int = 20,
        autostart: bool = True,
    ):
        self.store = store
        self.queue = queue
        self.poll_seconds = max(0.01, poll_seconds)
        self.batch_size = max(1, min(batch_size, 500))
        self.lease_seconds = max(1.0, lease_seconds)
        self.max_attempts = max(1, max_attempts)
        self.owner = "%s:%d:%s" % (socket.gethostname(), os.getpid(), uuid.uuid4().hex[:12])
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = threading.Event()
        self._last_error_lock = threading.Lock()
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._run,
            name="evoagent-outbox",
            daemon=True,
        )
        if autostart:
            self._thread.start()
            self._started.wait(2)

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    @property
    def last_error(self) -> str:
        with self._last_error_lock:
            return self._last_error

    def notify(self) -> None:
        self._wake.set()

    def dispatch_once(self) -> int:
        messages = self.store.claim_outbox(
            self.owner,
            self.batch_size,
            self.lease_seconds,
            self.max_attempts,
        )
        published = 0
        for message in messages:
            try:
                if message.get("topic") != "review":
                    raise ValueError("unsupported outbox topic: %s" % message.get("topic"))
                self.queue.submit(message["payload"], message_id=str(message["message_key"]))
                if self.store.mark_outbox_published(str(message["id"]), self.owner):
                    metrics.inc("outbox_published_total")
                    published += 1
                else:
                    metrics.inc("outbox_lease_conflicts_total")
            except Exception as exc:
                attempts = int(message.get("attempts", 1))
                delay = min(float(2 ** max(0, attempts - 1)), 30.0)
                self.store.release_outbox(
                    str(message["id"]),
                    self.owner,
                    safe_exception_summary(exc, "outbox dispatch failed"),
                    delay,
                    self.max_attempts,
                )
                metrics.inc("outbox_publish_failures_total")
                self._set_error(exc)
        if published:
            self._set_error(None)
        return published

    def stats(self) -> dict[str, Any]:
        return {
            **self.store.outbox_stats(),
            "dispatcher_running": self.running,
            "last_error": self.last_error,
        }

    def close(self, timeout_seconds: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(max(0.0, timeout_seconds))
        return not self._thread.is_alive()

    def _run(self) -> None:
        self._started.set()
        while not self._stop.is_set():
            try:
                published = self.dispatch_once()
                if published:
                    continue
            except Exception as exc:  # database outage; preserve rows and retry
                metrics.inc("outbox_dispatch_failures_total")
                self._set_error(exc)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _set_error(self, error: Exception | None) -> None:
        with self._last_error_lock:
            self._last_error = (
                "" if error is None else safe_exception_summary(error, "outbox dispatch failed")
            )
