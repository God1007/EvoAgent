import os
import tempfile
import unittest

from evoagent.errors import (
    coerce_safe_summary,
    preserve_safe_summary,
    safe_exception_fields,
    safe_exception_summary,
)
from evoagent.models import TaskState, TraceEvent
from evoagent.observability import Observability
from evoagent.store import TaskStore, utc_now


def _raise_at_stable_site(message):
    raise RuntimeError(message)


class OperationalErrorContractTests(unittest.TestCase):
    def test_summary_is_message_free_and_stable_for_the_same_failure_site(self):
        messages = ("password=first-secret", "password=second-secret")
        summaries = []
        fields = []
        for message in messages:
            try:
                _raise_at_stable_site(message)
            except RuntimeError as exc:
                summaries.append(safe_exception_summary(exc, "review execution failed"))
                fields.append(safe_exception_fields(exc))

        self.assertEqual(summaries[0], summaries[1])
        self.assertEqual(fields[0], fields[1])
        self.assertRegex(
            summaries[0],
            r"^review execution failed \[type=builtins\.RuntimeError; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn("first-secret", str((summaries, fields)))
        self.assertNotIn("second-secret", str((summaries, fields)))

    def test_untrusted_strings_are_replaced_while_safe_summaries_are_preserved(self):
        raw = "database-url=postgres://admin:secret@example"
        coerced = coerce_safe_summary(raw, "store readiness failed")
        self.assertRegex(
            coerced,
            r"^store readiness failed \[type=unknown; ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn(raw, coerced)

        safe = safe_exception_summary(RuntimeError(raw), "task delivery failed")
        self.assertEqual(safe, preserve_safe_summary(safe, "review execution failed"))
        self.assertNotEqual(safe, coerce_safe_summary(safe, "queue dependency failed"))

    def test_sqlite_persistence_boundary_rejects_raw_operational_errors(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        store = TaskStore(path)
        secret = "credential=must-never-be-persisted"
        now = utc_now()
        store.create_review_task(
            "task",
            "acme/widgets",
            1,
            {},
            "tenant",
            "--- a/a.py\n+++ b/a.py\n",
            {"task_id": "task"},
        )
        store.fail(
            "task",
            secret,
            TraceEvent(1, TaskState.FAILED, secret, now),
        )
        store.save_checkpoint("task", "executing", {}, "failed", 1, secret)
        store.record_failure_case("task", "execution_error", {"error": secret, "detail": secret})
        store.record_agent_message(
            "task",
            {
                "sender": "security-agent",
                "recipient": "planner-agent",
                "kind": "agent_failure",
                "content": {"error": secret, "detail": secret},
            },
        )
        store.audit("tenant", "system", "shadow.failed", "task", {"error": secret})
        store.create_alert("tenant", "dlq:task", "critical", secret)
        self.assertEqual("acquired", store.claim_effect("effect", "worker", 30)["status"])
        self.assertTrue(store.release_effect("effect", "worker", secret))
        claimed = store.claim_outbox("worker", 1, 30, 3)
        self.assertEqual(1, len(claimed))
        self.assertTrue(store.release_outbox(claimed[0]["id"], "worker", secret, 0, 1))

        with store._connect() as conn:
            effect_error = conn.execute(
                "SELECT last_error FROM effect_receipts WHERE effect_key='effect'"
            ).fetchone()[0]
        persisted = {
            "task": store.get("task"),
            "checkpoint": store.load_checkpoints("task"),
            "failure_cases": store.list_failure_cases(),
            "outbox": store.list_outbox("dead"),
            "effect_error": effect_error,
            "audit": store.list_audit("tenant"),
            "alerts": store.list_alerts("tenant"),
        }
        self.assertNotIn(secret, str(persisted))
        self.assertIn("review execution failed [type=unknown;", str(persisted))
        self.assertIn("review node failed [type=unknown;", str(persisted))
        self.assertIn("review agent failed [type=unknown;", str(persisted))
        self.assertIn("outbox dispatch failed [type=unknown;", str(persisted))
        self.assertIn("external effect failed [type=unknown;", str(persisted))
        self.assertIn("shadow review failed [type=unknown;", str(persisted))
        self.assertIn("task delivery failed [type=unknown;", str(persisted))


class _FakeSpan:
    def __init__(self):
        self.attributes = {}
        self.events = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes):
        self.events.append((name, attributes))


class _SpanContext:
    def __init__(self, span):
        self.span = span

    def __enter__(self):
        return self.span

    def __exit__(self, _error_type, _error, _traceback):
        return False


class _FakeTracer:
    def __init__(self):
        self.span = _FakeSpan()
        self.kwargs = {}

    def start_as_current_span(self, _name, **kwargs):
        self.kwargs = kwargs
        return _SpanContext(self.span)


class ObservabilityErrorContractTests(unittest.TestCase):
    def test_span_disables_sdk_exception_capture_and_records_only_safe_fields(self):
        observability = object.__new__(Observability)
        tracer = _FakeTracer()
        observability.tracer = tracer
        secret = "authorization=trace-secret"

        with self.assertRaises(RuntimeError):
            with observability.span("review.executing", "trace-id"):
                raise RuntimeError(secret)

        self.assertFalse(tracer.kwargs["record_exception"])
        self.assertFalse(tracer.kwargs["set_status_on_exception"])
        self.assertEqual("builtins.RuntimeError", tracer.span.attributes["error.type"])
        self.assertRegex(tracer.span.attributes["error.ref"], r"^[0-9a-f]{16}$")
        self.assertEqual("evoagent.failure", tracer.span.events[0][0])
        self.assertNotIn(secret, str((tracer.span.attributes, tracer.span.events)))


if __name__ == "__main__":
    unittest.main()
