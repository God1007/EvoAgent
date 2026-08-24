"""Circuit breaker + retry-with-backoff for outbound dependencies.

When GitHub or the LLM endpoint is down, naively retrying ties up worker threads
and turns a dependency outage into a full outage. The breaker trips after a burst
of failures and then *fails fast* for a cooldown window, letting the service stay
responsive (and shed the affected work) instead of hanging.

State machine:

* CLOSED: calls flow; consecutive failures are counted.
* OPEN: calls fail immediately with ``CircuitOpenError`` until the reset window
  elapses.
* HALF_OPEN: a limited number of trial calls are allowed; one success closes the
  breaker, a failure re-opens it.
"""

import math
import random
import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is open."""


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        half_open_max: int = 1,
    ):
        if (
            isinstance(failure_threshold, bool)
            or not isinstance(failure_threshold, int)
            or failure_threshold <= 0
        ):
            raise ValueError("circuit breaker failure threshold must be a positive integer")
        if (
            isinstance(reset_seconds, bool)
            or not isinstance(reset_seconds, (int, float))
            or not math.isfinite(reset_seconds)
            or reset_seconds < 0
        ):
            raise ValueError("circuit breaker reset seconds must be finite and non-negative")
        if (
            isinstance(half_open_max, bool)
            or not isinstance(half_open_max, int)
            or half_open_max <= 0
        ):
            raise ValueError("circuit breaker half-open limit must be a positive integer")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = float(reset_seconds)
        self.half_open_max = half_open_max
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def state_code(self) -> int:
        """0 closed, 1 half-open, 2 open (for gauge export)."""
        return {self.CLOSED: 0, self.HALF_OPEN: 1, self.OPEN: 2}[self.state]

    def allow(self) -> None:
        """Admission check. Raises ``CircuitOpenError`` if the call must be
        rejected; transitions OPEN -> HALF_OPEN once the cooldown elapses and
        reserves a half-open probe slot."""
        with self._lock:
            if self._state == self.OPEN:
                if (time.monotonic() - self._opened_at) >= self.reset_seconds:
                    self._state = self.HALF_OPEN
                    self._half_open_calls = 0
                else:
                    raise CircuitOpenError("circuit '%s' is open" % self.name)
            if self._state == self.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max:
                    raise CircuitOpenError("circuit '%s' is half-open and saturated" % self.name)
                self._half_open_calls += 1

    def record_success(self) -> None:
        self._on_success()

    def record_failure(self) -> None:
        self._on_failure()

    def call(self, fn: Callable[[], T]) -> T:
        self.allow()
        try:
            result = fn()
        except Exception:
            self._on_failure()
            raise
        self._on_success()
        return result

    def _on_success(self) -> None:
        with self._lock:
            if self._state == self.HALF_OPEN:
                # A probe succeeded: close and start fresh.
                self._state = self.CLOSED
                self._failures = 0
                self._half_open_calls = 0
            elif self._state == self.CLOSED:
                self._failures = 0
            # If OPEN, this is a late success from a call that began before the
            # breaker tripped; it must NOT resurrect the breaker to CLOSED.

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()


def call_with_retries(
    fn: Callable[[], T],
    *,
    retries: int = 2,
    base: float = 0.2,
    cap: float = 2.0,
    breaker: CircuitBreaker | None = None,
    retry_on: type[BaseException] | tuple[type[BaseException], ...] = Exception,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` (optionally through ``breaker``) with exponential backoff and
    jitter. A tripped breaker (``CircuitOpenError``) is raised immediately - the
    whole point is to fail fast rather than retry into a known-dead dependency."""
    attempt = 0
    while True:
        try:
            return breaker.call(fn) if breaker else fn()
        except CircuitOpenError:
            raise
        except retry_on:
            attempt += 1
            if attempt > retries:
                raise
            delay = min(cap, base * (2 ** (attempt - 1)))
            sleep(delay + random.uniform(0.0, delay * 0.1))
