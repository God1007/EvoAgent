import unittest

from evoagent.circuit_breaker import CircuitBreaker, CircuitOpenError, call_with_retries


def _boom():
    raise RuntimeError("dependency down")


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker("t", failure_threshold=2, reset_seconds=999)
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                breaker.call(_boom)
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)
        # Now fails fast without invoking the function.
        with self.assertRaises(CircuitOpenError):
            breaker.call(lambda: 1 / 0)

    def test_half_open_success_closes(self):
        breaker = CircuitBreaker("t", failure_threshold=1, reset_seconds=0)
        with self.assertRaises(RuntimeError):
            breaker.call(_boom)
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)
        # reset_seconds=0 -> next call is a half-open trial; success closes it.
        self.assertEqual("ok", breaker.call(lambda: "ok"))
        self.assertEqual(CircuitBreaker.CLOSED, breaker.state)

    def test_half_open_failure_reopens(self):
        breaker = CircuitBreaker("t", failure_threshold=1, reset_seconds=0)
        with self.assertRaises(RuntimeError):
            breaker.call(_boom)
        with self.assertRaises(RuntimeError):
            breaker.call(_boom)  # half-open trial fails -> reopen
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)

    def test_late_success_during_open_does_not_close(self):
        # A call that began while CLOSED can complete after the breaker trips;
        # its success must not resurrect the breaker.
        breaker = CircuitBreaker("t", failure_threshold=1, reset_seconds=999)
        with self.assertRaises(RuntimeError):
            breaker.call(_boom)
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)
        breaker.record_success()  # late in-flight success
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)

    def test_allow_rejects_when_open(self):
        breaker = CircuitBreaker("t", failure_threshold=1, reset_seconds=999)
        breaker.record_failure()
        with self.assertRaises(CircuitOpenError):
            breaker.allow()

    def test_manual_transport_pattern_only_counts_failures(self):
        # Mirrors github/reviewer usage: allow() + record_success/failure.
        breaker = CircuitBreaker("t", failure_threshold=2, reset_seconds=999)
        for _ in range(5):
            breaker.allow()
            breaker.record_success()  # "alive" responses never trip
        self.assertEqual(CircuitBreaker.CLOSED, breaker.state)
        breaker.allow()
        breaker.record_failure()
        breaker.allow()
        breaker.record_failure()
        self.assertEqual(CircuitBreaker.OPEN, breaker.state)

    def test_state_code_mapping(self):
        breaker = CircuitBreaker("t", failure_threshold=1, reset_seconds=999)
        self.assertEqual(0, breaker.state_code())
        with self.assertRaises(RuntimeError):
            breaker.call(_boom)
        self.assertEqual(2, breaker.state_code())


class RetryTests(unittest.TestCase):
    def test_succeeds_after_transient_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return "ok"

        result = call_with_retries(flaky, retries=3, sleep=lambda _d: None)
        self.assertEqual("ok", result)
        self.assertEqual(3, calls["n"])

    def test_raises_after_exhausting_retries(self):
        with self.assertRaises(RuntimeError):
            call_with_retries(_boom, retries=2, sleep=lambda _d: None)

    def test_open_breaker_is_not_retried(self):
        breaker = CircuitBreaker("t", failure_threshold=1, reset_seconds=999)
        with self.assertRaises(RuntimeError):
            breaker.call(_boom)  # trips the breaker

        attempts = {"n": 0}

        def counted():
            attempts["n"] += 1
            return "never"

        with self.assertRaises(CircuitOpenError):
            call_with_retries(counted, retries=5, breaker=breaker, sleep=lambda _d: None)
        self.assertEqual(0, attempts["n"])  # failed fast, no execution


if __name__ == "__main__":
    unittest.main()
