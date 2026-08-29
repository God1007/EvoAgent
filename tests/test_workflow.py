import copy
import http.client
import io
import itertools
import json
import tempfile
import threading
import tracemalloc
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock

from evoagent.agents import FINDINGS_TYPE, MultiAgentCoordinator, review_workflow
from evoagent.api import _make_server
from evoagent.config import Settings
from evoagent.diff_parser import parse_unified_diff
from evoagent.harness import ReviewHarness
from evoagent.metrics import Metrics
from evoagent.model_gateway import ModelGovernanceContext
from evoagent.models import (
    Finding,
    RepositoryEvidence,
    ReviewContext,
    Severity,
    TaskState,
    TraceEvent,
)
from evoagent.postgres_store import PostgresTaskStore
from evoagent.reviewer import GatewayReviewer, LocalRuleReviewer
from evoagent.service import ReviewService
from evoagent.time_utils import utc_now
from evoagent.workflow import (
    MAX_WORKFLOW_STEPS,
    AgentSpec,
    BudgetExceeded,
    HandoffError,
    PayloadType,
    Step,
    TaskCancelled,
    Workflow,
    _json,
)
from tests.db_support import postgres_store

REVISION = "a" * 64
DIFF = "--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+eval(value)\n"


def integer(value):
    if type(value) is not int:
        raise ValueError("integer required")


NUMBER = PayloadType("number", 1, integer)


def agent(name="increment", handler=None, inputs=None):
    return AgentSpec(
        name,
        REVISION,
        {"value": NUMBER} if inputs is None else inputs,
        {"value": NUMBER},
        handler or (lambda handoff: {"value": handoff.inputs["value"] + 1}),
    )


def chain(first=None, second=None):
    return Workflow(
        "numbers",
        {"value": NUMBER},
        (
            Step("first", first or agent(), {"value": "$input.value"}),
            Step("second", second or agent(), {"value": "first.value"}),
        ),
        {"value": "second.value"},
    )


class Checkpoints:
    """Test double for the existing first-write-wins, generation-fenced SQL API."""

    def __init__(self):
        self.records = {}
        self.generation = 1
        self.cancelled = False
        self.messages = []
        self.on_save = None
        self.reads = []
        self.lock = threading.RLock()

    def __deepcopy__(self, memo):
        clone = type(self)()
        memo[id(self)] = clone
        with self.lock:
            for key, value in self.__dict__.items():
                if key != "lock":
                    setattr(clone, key, copy.deepcopy(value, memo))
        return clone

    def is_cancelled(self, _task):
        with self.lock:
            return self.cancelled

    def review_admission_active(self, _task, generation):
        with self.lock:
            return generation == self.generation

    def load_checkpoints(self, _task, node=None):
        with self.lock:
            self.reads.append(node)
            return copy.deepcopy(
                {key: value for key, value in self.records.items() if node is None or key == node}
            )

    def save_checkpoint(
        self, _task, node, state, status="completed", attempt=1, error="", generation=None
    ):
        with self.lock:
            if self.cancelled or (generation is not None and generation != self.generation):
                return False
            if self.on_save:
                self.on_save(node, state, status)
            prior = self.records.get(node, {})
            if prior.get("status") != "completed" and (
                status == "completed" or attempt >= prior.get("attempt", 0)
            ):
                self.records[node] = copy.deepcopy(
                    {"state": state, "status": status, "attempt": attempt, "error": error}
                )
            return True

    def record_agent_message(self, _task, message, _generation=None):
        self.messages.append(message)
        return True


def durable(flow, store, value=1, generation=1, execution_revision=REVISION):
    return flow.run(
        {"value": value},
        task_id="task",
        store=store,
        generation=generation,
        execution_revision=execution_revision,
    )


class WorkflowTests(unittest.TestCase):
    def test_finding_handoffs_reject_lossy_legacy_normalization_before_publication(self):
        finding = Finding(
            "SEC-EVAL", Severity.HIGH, "eval", "explanation", "a.py", 1, "eval(x)", "fix", "test"
        ).to_dict()
        receiver = mock.Mock(side_effect=lambda h: dict(h.inputs))
        for field, value in (
            ("severity", "not-a-severity"),
            ("severity", "HIGH"),
            ("severity", None),
            ("fingerprint", "wrong"),
            ("permissions", {"manage": True}),
        ):
            with self.subTest(field=field, value=value):
                producer = AgentSpec(
                    "produce",
                    REVISION,
                    {},
                    {"findings": FINDINGS_TYPE},
                    lambda _h, payload={**finding, field: value}: {"findings": [payload]},
                )
                consumer = replace(
                    producer, agent_id="consume", inputs=producer.outputs, run=receiver
                )
                flow = Workflow(
                    "findings",
                    {},
                    (
                        Step("produce", producer, {}),
                        Step("consume", consumer, {"findings": "produce.findings"}),
                    ),
                    {"findings": "consume.findings"},
                )
                store = Checkpoints()
                with self.assertRaises(HandoffError):
                    flow.run({}, task_id="task", store=store, execution_revision=REVISION)
                self.assertEqual("failed", store.records["workflow:produce"]["status"])
                self.assertNotIn("outputs", store.records["workflow:produce"]["state"])
                receiver.assert_not_called()
        # Both existing dataclass adapters and public Finding.to_dict() remain valid.
        FINDINGS_TYPE.validate([finding])
        FINDINGS_TYPE.validate(
            [{key: value for key, value in finding.items() if key != "fingerprint"}]
        )

    def test_compiled_workflow_cannot_drift_from_its_revision(self):
        flow = chain()
        manifest = flow.describe()
        for field in (
            "name",
            "steps",
            "inputs",
            "outputs",
            "output_types",
            "revision",
            "_order",
            "_waves",
        ):
            with self.subTest(field=field), self.assertRaises(FrozenInstanceError):
                setattr(flow, field, None)
        with self.assertRaises(FrozenInstanceError):
            del flow.revision
        self.assertEqual(manifest, flow.describe())
        self.assertEqual({"value": 3}, flow.run({"value": 1}))
        changed = replace(flow, name="renamed")
        self.assertNotEqual(flow.revision, changed.revision)
        self.assertEqual({"value": 3}, changed.run({"value": 1}))

    def test_fanout_join_config_and_input_isolation(self):
        data = PayloadType("json-object", 1, lambda value: None)

        def mutate(handoff):
            handoff.inputs["value"]["items"].append("local")
            with self.assertRaises(TypeError):
                handoff.inputs["extra"] = True
            return {"value": len(handoff.inputs["value"]["items"])}

        agents = {
            "mutate": agent("mutate", mutate, {"value": data}),
            "observe": agent(
                "observe", lambda h: {"value": len(h.inputs["value"]["items"])}, {"value": data}
            ),
            "join": agent(
                "join",
                lambda h: {"value": h.inputs["left"] + h.inputs["right"]},
                {"left": NUMBER, "right": NUMBER},
            ),
        }
        definition = {
            "name": "branches",
            "steps": [
                {
                    "id": "join",
                    "agent": "join",
                    "sources": {"left": "left.value", "right": "right.value"},
                },
                {"id": "left", "agent": "mutate", "sources": {"value": "$input.value"}},
                {"id": "right", "agent": "observe", "sources": {"value": "$input.value"}},
            ],
            "outputs": {"value": "join.value", "branch": "left.value", "original": "$input.value"},
        }
        flow = Workflow.from_dict(definition, agents, {"value": data})
        definition["steps"][0]["sources"]["right"] = "missing.value"
        original = {"value": {"items": ["original"]}}
        self.assertEqual(
            {"value": 3, "branch": 2, "original": {"items": ["original"]}}, flow.run(original)
        )
        self.assertEqual({"value": {"items": ["original"]}}, original)

    def test_parallel_branch_failure_keeps_completed_sibling_for_resume(self):
        barrier = threading.Barrier(2)
        calls = {"left": 0, "right": 0, "join": 0}

        def branch(name, increment):
            def run(handoff):
                calls[name] += 1
                if calls[name] == 1:
                    barrier.wait(timeout=1)
                if name == "right" and calls[name] == 1:
                    raise RuntimeError("retry right branch")
                return {"value": handoff.inputs["value"] + increment}

            return agent(name, run)

        def join(handoff):
            calls["join"] += 1
            return {"value": handoff.inputs["left"] + handoff.inputs["right"]}

        flow = Workflow(
            "parallel-resume",
            {"value": NUMBER},
            (
                Step(
                    "join",
                    agent("join", join, {"left": NUMBER, "right": NUMBER}),
                    {"left": "left.value", "right": "right.value"},
                ),
                Step("left", branch("left", 1), {"value": "$input.value"}),
                Step("right", branch("right", 2), {"value": "$input.value"}),
            ),
            {"value": "join.value"},
        )
        store = Checkpoints()
        with self.assertRaisesRegex(RuntimeError, "retry right branch"):
            durable(flow, store)
        self.assertEqual("completed", store.records["workflow:left"]["status"])
        self.assertEqual("failed", store.records["workflow:right"]["status"])
        self.assertNotIn("workflow:join", store.records)

        self.assertEqual({"value": 5}, durable(flow, store))
        self.assertEqual({"left": 1, "right": 2, "join": 1}, calls)

    def test_parallel_wave_stops_before_join_at_the_shared_deadline(self):
        release = threading.Event()
        finished = [threading.Event(), threading.Event()]

        def blocked(index):
            def run(handoff):
                try:
                    release.wait(timeout=1)
                    return {"value": handoff.inputs["value"] + 1}
                finally:
                    finished[index].set()

            return agent("blocked%d" % index, run)

        join = mock.Mock(return_value={"value": 4})
        flow = Workflow(
            "parallel-deadline",
            {"value": NUMBER},
            (
                Step("left", blocked(0), {"value": "$input.value"}),
                Step("right", blocked(1), {"value": "$input.value"}),
                Step(
                    "join",
                    agent("join", join, {"left": NUMBER, "right": NUMBER}),
                    {"left": "left.value", "right": "right.value"},
                ),
            ),
            {"value": "join.value"},
        )
        try:
            with self.assertRaises(BudgetExceeded):
                flow.run({"value": 1}, timeout_seconds=0.02)
        finally:
            release.set()
            self.assertTrue(all(event.wait(timeout=1) for event in finished))
        join.assert_not_called()

    def test_chain_memory_does_not_retain_consumed_outputs(self):
        text = PayloadType("text", 1, lambda value: None)
        echo = AgentSpec("echo", REVISION, {"text": text}, {"text": text}, lambda h: dict(h.inputs))
        payload = {"text": "x" * (512 * 1024)}

        def peak(length):
            flow = Workflow(
                "chain",
                {"text": text},
                [
                    Step(
                        "node%d" % index,
                        echo,
                        {"text": "$input.text" if index == 0 else "node%d.text" % (index - 1)},
                    )
                    for index in range(length)
                ],
                {"text": "node%d.text" % (length - 1)},
            )
            tracemalloc.start()
            try:
                self.assertEqual(payload, flow.run(payload))
                return tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()

        short = peak(4)
        self.assertLess(peak(32), short + 4 * len(payload["text"]))

    def test_invalid_graphs_fail_before_execution(self):
        value = agent()
        incompatible = replace(value, inputs={"value": PayloadType("number", 2, integer)})
        for steps in (
            [Step("one", value, {"value": "missing.value"})],
            [Step("one", value, {"value": "$input.missing"})],
            [
                Step("one", value, {"value": "two.value"}),
                Step("two", value, {"value": "one.value"}),
            ],
            [Step("one", value, {"value": "$input.value"})] * 2,
            [Step("one", incompatible, {"value": "$input.value"})],
            [Step("one", value, {"value": "__import__('os')"})],
            [],
            [
                Step("step%d" % n, value, {"value": "$input.value"})
                for n in range(MAX_WORKFLOW_STEPS + 1)
            ],
        ):
            with self.subTest(steps=len(steps)), self.assertRaises(ValueError):
                Workflow("invalid", {"value": NUMBER}, steps, {"value": "one.value"})
        with self.assertRaisesRegex(ValueError, "exactly"):
            Step("one", value, {})
        with self.assertRaisesRegex(ValueError, "not registered"):
            Workflow.from_dict(
                {
                    "name": "invalid",
                    "steps": [{"id": "one", "agent": "os.system", "sources": {}}],
                    "outputs": {"value": "one.value"},
                },
                {},
                {},
            )

    def test_registration_and_limits_fail_closed(self):
        for name, value in (
            ("revision", "mutable"),
            ("agent_id", "two words"),
            ("kind", "two words"),
            ("run", None),
            ("outputs", {}),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                replace(agent(), **{name: value})
        for value in (True, 0, -1):
            with self.assertRaises(ValueError):
                PayloadType("bad", value, integer)
        for timeout in (True, 0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                chain().run({"value": 1}, timeout_seconds=timeout)
        with self.assertRaises(ValueError):
            chain().run({"value": 1}, store=Checkpoints())

    def test_agent_metrics_use_bounded_kinds_and_skip_checkpoint_replays(self):
        captured = Metrics(buckets=(1.0,))
        flow = Workflow(
            "studio",
            {"value": NUMBER},
            (Step("rule", replace(agent(), kind="rules"), {"value": "$input.value"}),),
            {"value": "rule.value"},
        )
        store = Checkpoints()
        clock = itertools.count(0.0, 0.25)
        with (
            mock.patch("evoagent.workflow.metrics", captured),
            mock.patch("evoagent.workflow.time.monotonic", side_effect=lambda: next(clock)),
        ):
            self.assertEqual({"value": 2}, durable(flow, store))
            self.assertEqual({"value": 2}, durable(flow, store))

        output = captured.prometheus()
        self.assertIn("evoagent_workflow_studio_agent_rules_runs_total 1.0", output)
        self.assertIn("evoagent_workflow_studio_agent_rules_successes_total 1.0", output)
        self.assertIn("evoagent_workflow_studio_agent_rules_duration_count 1", output)
        self.assertIn("evoagent_workflow_studio_agent_rules_checkpoint_reuses_total 1.0", output)
        state = store.records["workflow:rule"]["state"]
        self.assertTrue(state["started_at"].endswith("+00:00"))
        self.assertEqual(250, state["duration_ms"])

    def test_agent_metrics_collapse_untrusted_identity_and_record_failures(self):
        captured = Metrics()
        broken = replace(
            agent("tenant-specific-agent", lambda _handoff: 1 / 0), kind="tenant-specific-kind"
        )
        flow = Workflow(
            "tenant-specific-workflow",
            {"value": NUMBER},
            (Step("tenant-specific-step", broken, {"value": "$input.value"}),),
            {"value": "tenant-specific-step.value"},
        )
        with (
            mock.patch("evoagent.workflow.metrics", captured),
            self.assertRaises(ZeroDivisionError),
        ):
            flow.run({"value": 1})

        output = captured.prometheus()
        self.assertIn("evoagent_workflow_custom_agent_custom_runs_total 1.0", output)
        self.assertIn("evoagent_workflow_custom_agent_custom_failures_total 1.0", output)
        self.assertNotIn("tenant_specific", output)

    def test_coordinator_records_workflow_outcome_and_duration(self):
        captured = Metrics()
        coordinator = MultiAgentCoordinator([LocalRuleReviewer()])
        with mock.patch("evoagent.agents.metrics", captured):
            coordinator.review(DIFF, parse_unified_diff(DIFF))

        output = captured.prometheus()
        self.assertIn("evoagent_workflow_review_runs_total 1.0", output)
        self.assertIn("evoagent_workflow_review_successes_total 1.0", output)
        self.assertIn("evoagent_workflow_review_duration_count 1", output)

    def test_invalid_output_never_reaches_receiver(self):
        downstream = mock.Mock(return_value={"value": 1})
        for output in (
            None,
            {},
            {"value": True},
            {"value": float("nan")},
            {"value": 1, "recipient": "admin"},
        ):
            with self.subTest(output=output), self.assertRaises(HandoffError):
                chain(
                    agent(handler=lambda _h, result=output: result), agent(handler=downstream)
                ).run({"value": 1})
        downstream.assert_not_called()

    def test_json_wire_format_is_strict_and_bounded(self):
        data = PayloadType("json", 1, lambda value: None)
        passthrough = AgentSpec(
            "echo", REVISION, {"value": data}, {"value": data}, lambda h: dict(h.inputs)
        )
        flow = Workflow(
            "json",
            {"value": data},
            [Step("echo", passthrough, {"value": "$input.value"})],
            {"value": "echo.value"},
        )
        for value in (b"bytes", {1: "key"}, (1, 2), "\x00", "\ud800", float("inf")):
            with self.subTest(value=repr(value)), self.assertRaises(HandoffError):
                flow.run({"value": value})
        with (
            mock.patch("evoagent.workflow.MAX_HANDOFF_BYTES", 100),
            self.assertRaisesRegex(HandoffError, "size"),
        ):
            flow.run({"value": "x" * 200})

        store, receiver = Checkpoints(), mock.Mock()
        with (
            mock.patch("evoagent.workflow.MAX_HANDOFF_BYTES", 4096),
            self.assertRaisesRegex(HandoffError, "size"),
        ):
            durable(
                chain(agent(handler=lambda h: {"value": "x" * 5000}), agent(handler=receiver)),
                store,
            )
        self.assertEqual("failed", store.records["workflow:first"]["status"])
        self.assertNotIn("outputs", store.records["workflow:first"]["state"])
        self.assertNotIn("workflow:second", store.records)
        receiver.assert_not_called()

    def test_json_encoding_preserves_bytes_and_stops_before_full_expansion(self):
        for value in (
            None,
            True,
            False,
            0,
            -1,
            0.0,
            -0.0,
            1e-20,
            1e30,
            [],
            {},
            {"z": 'é😀\n\t\\"', "a": [1, None, {"control": "\x01"}]},
        ):
            expected = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
            ).encode()
            with (
                self.subTest(value=value),
                mock.patch("evoagent.workflow.MAX_HANDOFF_BYTES", len(expected)),
            ):
                self.assertEqual(expected, _json(value))
            with (
                mock.patch("evoagent.workflow.MAX_HANDOFF_BYTES", len(expected) - 1),
                self.assertRaisesRegex(HandoffError, "size"),
            ):
                _json(value)

        # Escapes expand beyond the byte limit even though raw characters fit.
        value = {"port%d" % i: "\x01" * (10 * 1024) for i in range(16)}
        limit = 256 * 1024
        with mock.patch("evoagent.workflow.MAX_HANDOFF_BYTES", limit):
            tracemalloc.start()
            try:
                with self.assertRaisesRegex(HandoffError, "size"):
                    _json(value)
                self.assertLess(tracemalloc.get_traced_memory()[1], 4 * limit)
            finally:
                tracemalloc.stop()

    def test_json_preflight_bounds_shared_subtrees_before_encoding(self):
        value = [0]
        for _ in range(16):
            value = [value, value]
        with (
            mock.patch("evoagent.workflow.MAX_HANDOFF_BYTES", 1024),
            mock.patch(
                "evoagent.workflow.json.dumps",
                side_effect=AssertionError("must reject before encoding"),
            ),
            mock.patch(
                "evoagent.workflow.json.JSONEncoder.iterencode",
                side_effect=AssertionError("must reject before encoding"),
            ),
            self.assertRaisesRegex(HandoffError, "size"),
        ):
            _json(value)

    def test_receiver_revalidates_even_when_contract_ids_match(self):
        def stricter(value):
            if value < 10:
                raise ValueError("too small")

        downstream = mock.Mock(return_value={"value": 10})
        second = replace(
            agent(handler=downstream), inputs={"value": PayloadType("number", 1, stricter)}
        )
        with self.assertRaises(HandoffError):
            chain(second=second).run({"value": 1})
        downstream.assert_not_called()

    def test_validators_cannot_silently_change_the_hashed_payload(self):
        def mutate(value):
            value.append("changed")

        data = PayloadType("items", 1, lambda value: None)
        mutated = replace(data, validate=mutate)
        handler = mock.Mock(side_effect=lambda h: dict(h.inputs))
        for boundary in ("root", "input", "output"):
            with self.subTest(boundary=boundary):
                handler.reset_mock()
                echo = AgentSpec(
                    "echo",
                    REVISION,
                    {"value": mutated if boundary == "input" else data},
                    {"value": mutated if boundary == "output" else data},
                    handler,
                )
                flow = Workflow(
                    "items",
                    {"value": mutated if boundary == "root" else data},
                    [Step("echo", echo, {"value": "$input.value"})],
                    {"value": "echo.value"},
                )
                original = {"value": ["original"]}
                with self.assertRaisesRegex(HandoffError, "must not modify"):
                    flow.run(original)
                self.assertEqual({"value": ["original"]}, original)
                self.assertEqual(int(boundary == "output"), handler.call_count)

    def test_failure_resumes_at_receiver_with_stable_idempotency_key(self):
        store = Checkpoints()
        seen = []
        first = mock.Mock(side_effect=lambda h: {"value": h.inputs["value"] + 1})

        def flaky(handoff):
            seen.append(handoff)
            if len(seen) == 1:
                raise RuntimeError("private upstream credentials")
            return {"value": handoff.inputs["value"] + 1}

        flow = chain(agent(handler=first), agent(handler=flaky))
        with self.assertRaises(RuntimeError):
            durable(flow, store)
        self.assertNotIn("private upstream credentials", store.records["workflow:second"]["error"])
        failed = store.records["workflow:second"]["state"]
        self.assertEqual("second", failed["handoff"]["step_id"])
        self.assertEqual(1, failed["generation"])
        self.assertEqual(seen[0].idempotency_key, failed["idempotency_key"])
        store.generation = 2
        self.assertEqual({"value": 3}, durable(flow, store, generation=2))
        first.assert_called_once()
        self.assertEqual([1, 2], [item.attempt for item in seen])
        self.assertEqual([1, 2], [item.generation for item in seen])
        self.assertEqual(seen[0].idempotency_key, seen[1].idempotency_key)
        self.assertEqual({"value": "first.value"}, seen[1].sources)
        self.assertEqual({"value": 3}, durable(flow, store, generation=2))
        self.assertEqual(2, len(seen))

    def test_changed_input_flow_or_execution_cannot_reuse_old_checkpoints(self):
        store = Checkpoints()
        durable(chain(), store)
        for flow, value, revision in (
            (chain(), 2, REVISION),
            (chain(), 1, "b" * 64),
            (chain(replace(agent(), revision="b" * 64)), 1, REVISION),
        ):
            with self.subTest(revision=revision), self.assertRaisesRegex(HandoffError, "changed"):
                durable(flow, store, value=value, execution_revision=revision)

    def test_corrupt_checkpoint_is_rejected_without_running_dependants(self):
        store = Checkpoints()
        flow = chain()
        durable(flow, store)
        for field, value in (
            ("outputs", {"value": 99}),
            ("idempotency_key", "wrong"),
            ("handoff", {}),
        ):
            corrupted = copy.deepcopy(store)
            corrupted.records["workflow:first"]["state"][field] = value
            with self.subTest(field=field), self.assertRaises(HandoffError):
                durable(flow, corrupted)

    def test_concurrent_loser_consumes_committed_winner_on_success_or_failure(self):
        winner_store = Checkpoints()
        durable(chain(), winner_store)
        winning_record = winner_store.records["workflow:first"]
        for result in ({"value": 99}, RuntimeError("lost"), ValueError("lost")):
            store = Checkpoints()

            def commit_winner(node, _state, _status, store=store):
                if node == "workflow:first" and _status != "running":
                    store.records[node] = copy.deepcopy(winning_record)

            store.on_save = commit_winner
            handler = (
                mock.Mock(side_effect=result)
                if isinstance(result, Exception)
                else mock.Mock(return_value=result)
            )
            with self.subTest(result=result):
                self.assertEqual({"value": 3}, durable(chain(agent(handler=handler)), store))
                handler.assert_called_once()

    def test_winner_committed_before_dispatch_skips_the_handler(self):
        winner = Checkpoints()
        durable(chain(), winner)
        store = Checkpoints()

        def commit_winner(node, _state, _status):
            if node == "workflow:first":
                store.records[node] = copy.deepcopy(winner.records[node])

        store.on_save = commit_winner
        handler = mock.Mock()
        self.assertEqual({"value": 3}, durable(chain(agent(handler=handler)), store))
        handler.assert_not_called()

    def test_cancel_or_new_generation_during_agent_prevents_handoff(self):
        for field, value in (("cancelled", True), ("generation", 2)):
            store = Checkpoints()

            def change_owner(_handoff, store=store, field=field, value=value):
                setattr(store, field, value)
                return {"value": 99}

            receiver = mock.Mock()
            with self.subTest(field=field), self.assertRaises(TaskCancelled):
                durable(chain(agent(handler=change_owner), agent(handler=receiver)), store)
            self.assertEqual("running", store.records["workflow:first"]["status"])
            self.assertNotIn("outputs", store.records["workflow:first"]["state"])
            receiver.assert_not_called()

    def test_cached_result_still_checks_the_active_generation(self):
        store = Checkpoints()
        flow = chain()
        durable(flow, store)
        store.generation = 2
        with self.assertRaises(TaskCancelled):
            durable(flow, store)

    def test_commit_fence_rejection_does_not_release_output(self):
        store = Checkpoints()
        save = store.save_checkpoint

        def reject(task, node, *args):
            return False if node == "workflow:first" else save(task, node, *args)

        store.save_checkpoint = reject
        receiver = mock.Mock()
        with self.assertRaises(TaskCancelled):
            durable(chain(second=agent(handler=receiver)), store)
        receiver.assert_not_called()

    def test_ambiguous_commit_replay_does_not_rerun_completed_agents(self):
        store = Checkpoints()
        save = store.save_checkpoint
        first = mock.Mock(return_value={"value": 2})

        def ambiguous(task, node, state, status, attempt, error, generation):
            result = save(task, node, state, status, attempt, error, generation)
            if node == "workflow:first" and status == "completed":
                raise OSError("connection lost after commit")
            return result

        store.save_checkpoint = ambiguous
        self.assertEqual({"value": 3}, durable(chain(agent(handler=first)), store))
        first.assert_called_once()

    def test_deadline_is_checked_before_commit_and_available_to_agent(self):
        store = Checkpoints()
        with mock.patch("evoagent.workflow.time.monotonic", return_value=0.0) as clock:

            def late(handoff):
                handoff.check_active()
                clock.return_value = handoff.deadline + 1
                return {"value": 2}

            with self.assertRaises(BudgetExceeded):
                durable(chain(agent(handler=late)), store)
        self.assertEqual("running", store.records["workflow:first"]["status"])
        self.assertNotIn("outputs", store.records["workflow:first"]["state"])

    def test_running_snapshot_precedes_handler_and_reads_are_node_scoped(self):
        store = Checkpoints()

        def inspect(handoff):
            record = store.records["workflow:first"]
            self.assertEqual("running", record["status"])
            self.assertEqual(handoff.idempotency_key, record["state"]["idempotency_key"])
            self.assertNotIn("inputs", record["state"])
            self.assertNotIn("outputs", record["state"])
            return {"value": 2}

        flow = chain(agent(handler=inspect))
        durable(flow, store)
        self.assertEqual(flow.describe(), store.records["workflow"]["state"]["definition"])
        self.assertNotIn(None, store.reads)
        self.assertEqual({"workflow", "workflow:first", "workflow:second"}, set(store.reads))

    def test_receiver_rejection_has_a_failed_checkpoint_without_dispatch(self):
        store = Checkpoints()

        def reject(_value):
            raise ValueError("private input in validation exception")

        handler = mock.Mock()
        receiver = replace(
            agent(handler=handler), inputs={"value": PayloadType("number", 1, reject)}
        )
        with self.assertRaises(HandoffError):
            durable(chain(second=receiver), store)
        self.assertEqual("failed", store.records["workflow:second"]["status"])
        self.assertNotIn("private input", str(store.records))
        handler.assert_not_called()

    def test_interrupted_running_agent_replays_the_same_handoff(self):
        store = Checkpoints()
        seen = []

        def interrupted(handoff):
            seen.append(handoff)
            if len(seen) == 1:
                raise KeyboardInterrupt()
            return {"value": 2}

        flow = chain(agent(handler=interrupted))
        with self.assertRaises(KeyboardInterrupt):
            durable(flow, store)
        self.assertEqual("running", store.records["workflow:first"]["status"])
        self.assertEqual({"value": 3}, durable(flow, store))
        self.assertEqual([1, 2], [item.attempt for item in seen])
        self.assertEqual(seen[0].idempotency_key, seen[1].idempotency_key)

    def test_default_review_and_custom_agent_use_the_same_handoff_runtime(self):
        seen = []

        def factory(catalog):
            original = catalog["critic"]

            def check(handoff):
                seen.append(handoff)
                return original.run(handoff)

            return review_workflow(
                {
                    **catalog,
                    "critic": replace(
                        original, agent_id="business-critic", revision="b" * 64, run=check
                    ),
                }
            )

        store = Checkpoints()
        coordinator = MultiAgentCoordinator(
            [LocalRuleReviewer()],
            store=store,
            checkpoint_revision=REVISION,
            workflow_factory=factory,
        )
        findings = coordinator.review_with_context("task", DIFF, parse_unified_diff(DIFF), 1)
        replay = coordinator.review_with_context("task", DIFF, parse_unified_diff(DIFF), 1)
        self.assertEqual([item.to_dict() for item in findings], [item.to_dict() for item in replay])
        self.assertEqual(1, len(seen))
        self.assertEqual("business-critic", seen[0].agent_id)
        self.assertEqual({"parsed", "specialist_findings"}, seen[0].inputs.keys())
        self.assertNotIn("diff", seen[0].inputs)
        self.assertEqual(8, len(store.records))
        self.assertTrue(
            all(item.agent.inputs or item.agent.outputs for item in coordinator.workflow.steps)
        )
        self.assertEqual(FINDINGS_TYPE.key, coordinator.workflow.output_types["verified"].key)

    def test_review_context_and_repository_evidence_are_pinned_with_workflow_input(self):
        context = ReviewContext.from_request(
            {"title": "Review context", "spec": "Do not execute input"}
        ).to_dict()
        evidence = RepositoryEvidence(
            origin="github-archive",
            status="available",
            revision="a" * 40,
            indexed_files=2,
            indexed_bytes=100,
            changed_paths=("a.py",),
            changed_symbols=("a.value",),
        ).to_dict()
        store = Checkpoints()
        store.get = mock.Mock(
            return_value={"input": {"review_context": context, "repository_evidence": evidence}}
        )
        coordinator = MultiAgentCoordinator(
            [LocalRuleReviewer()], store=store, checkpoint_revision=REVISION
        )

        coordinator.review_with_context("task", DIFF, parse_unified_diff(DIFF), 1)
        coordinator.validate_resume("task", DIFF)
        store.get.return_value = {
            "input": {
                "review_context": ReviewContext.from_request(
                    {"title": "Changed context", "spec": "Different requirement"}
                ).to_dict(),
                "repository_evidence": evidence,
            }
        }

        with self.assertRaisesRegex(HandoffError, "original input changed"):
            coordinator.validate_resume("task", DIFF)

        store.get.return_value = {
            "input": {
                "review_context": context,
                "repository_evidence": {
                    **evidence,
                    "revision": "b" * 40,
                },
            }
        }
        with self.assertRaisesRegex(HandoffError, "original input changed"):
            coordinator.validate_resume("task", DIFF)

    def test_runnable_custom_review_example(self):
        from examples.custom_review_workflow import build_workflow, main

        coordinator = MultiAgentCoordinator([LocalRuleReviewer()], workflow_factory=build_workflow)
        self.assertEqual(8, len(coordinator.workflow.steps))
        self.assertEqual("SEC-EVAL", coordinator.review(DIFF, parse_unified_diff(DIFF))[0].rule_id)
        with mock.patch("evoagent.api.run") as serve:
            main(["--serve"])
        serve.assert_called_once_with(workflow_factory=build_workflow)

    def test_configured_entrypoint_snapshots_wiring_and_checks_without_dispatch(self):
        from examples import custom_review_workflow as example

        source = Path(__file__).resolve().parents[1] / "examples/business-review.json"
        original = MultiAgentCoordinator(
            [LocalRuleReviewer()], workflow_factory=example.build_workflow
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flow.json"
            content = source.read_text()
            path.write_text(content)
            factory = example.load_workflow_factory(path)
            catalog = original.workflow_agents()
            self.assertEqual(original.workflow.revision, factory(catalog).revision)

            changed = json.loads(content)
            changed["name"] = "changed-review"
            path.write_text(json.dumps(changed))
            self.assertEqual(original.workflow.revision, factory(catalog).revision)
            self.assertNotEqual(
                factory(catalog).revision, example.load_workflow_factory(path)(catalog).revision
            )
            rebound = {**catalog, "critic": replace(catalog["critic"], run=mock.Mock())}
            self.assertIs(rebound["critic"], factory(rebound).steps[2].agent)

            with (
                mock.patch.object(LocalRuleReviewer, "review") as review,
                mock.patch.object(example, "business_policy") as policy,
                mock.patch("evoagent.api.run") as serve,
                mock.patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                example.main(["--workflow", str(source), "--check"])
                checked = json.loads(output.getvalue())
                self.assertTrue(checked["valid"])
                self.assertEqual(original.workflow.revision, checked["revision"])
                self.assertEqual(8, len(checked["workflow"]["steps"]))
                serve.assert_not_called()
                example.main(["--workflow", str(source), "--serve"])
                configured = serve.call_args.kwargs["workflow_factory"]
                self.assertEqual(original.workflow.revision, configured(catalog).revision)
                review.assert_not_called()
                policy.assert_not_called()

            for invalid in ("null", "[]", "{", '{"name":NaN}', '{"name":"one","name":"two"}'):
                path.write_text(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    example.load_workflow_factory(path)
            path.write_text(content)
            with mock.patch.object(example, "MAX_HANDOFF_BYTES", len(content.encode()) - 1):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    example.load_workflow_factory(path)
            changed["steps"][0]["agent"] = "os.system"
            path.write_text(json.dumps(changed))
            with (
                mock.patch("evoagent.api.run") as serve,
                mock.patch.object(LocalRuleReviewer, "review") as review,
                mock.patch("sys.stderr", new_callable=io.StringIO) as errors,
                self.assertRaises(SystemExit) as failure,
            ):
                example.main(["--workflow", str(path), "--serve"])
            self.assertEqual(2, failure.exception.code)
            self.assertIn("not registered", errors.getvalue())
            serve.assert_not_called()
            review.assert_not_called()


class WorkflowPostgresTests(unittest.TestCase):
    def test_numeric_handoffs_survive_persistence_and_receiver_restart_without_rehashing(self):
        store = postgres_store(self)
        task_id = "numeric-handoff"
        store.create(task_id, "demo/repo", 7, {})
        payload = {
            "negative_zero": -0.0,
            "large_float": 1e20,
            "large_integer": 10**100,
            "values": [0.0, 1.0, 0.95, 1e100, 1.7976931348623157e308, 5e-324, 1e-300],
        }
        expected = _json(payload)
        contract = PayloadType("numeric-json", 1, lambda value: None)
        producer = mock.Mock(side_effect=lambda h: dict(h.inputs))
        receipts = []

        def receive(handoff):
            self.assertEqual(expected, _json(handoff.inputs["value"]))
            receipts.append(handoff.idempotency_key)
            if len(receipts) == 1:
                raise RuntimeError("receiver interrupted")
            return dict(handoff.inputs)

        spec = AgentSpec("producer", REVISION, {"value": contract}, {"value": contract}, producer)
        flow = Workflow(
            "numeric",
            spec.inputs,
            (
                Step("first", spec, {"value": "$input.value"}),
                Step(
                    "second",
                    replace(spec, agent_id="receiver", run=receive),
                    {"value": "first.value"},
                ),
            ),
            {"value": "second.value"},
        )
        with self.assertRaisesRegex(RuntimeError, "receiver interrupted"):
            flow.run({"value": payload}, task_id=task_id, store=store, execution_revision=REVISION)
        original = store.load_checkpoints(task_id, "workflow:first")["workflow:first"]
        self.assertEqual(expected, _json(original["state"]["outputs"]["value"]))
        store.close()
        restored = PostgresTaskStore(store.url, pool_min=0, pool_max=0, auto_migrate=False)
        self.addCleanup(restored.close)
        result = flow.run(
            {"value": payload}, task_id=task_id, store=restored, execution_revision=REVISION
        )
        self.assertEqual(expected, _json(result["value"]))
        self.assertIs(type(result["value"]["large_float"]), float)
        self.assertIs(type(result["value"]["large_integer"]), int)
        self.assertEqual(
            original, restored.load_checkpoints(task_id, "workflow:first")["workflow:first"]
        )
        self.assertEqual([receipts[0], receipts[0]], receipts)
        producer.assert_called_once()
        artifact = restored.workflow_artifact("default", task_id, "first")
        self.assertEqual(expected, _json(artifact["outputs"]["value"]))
        snapshot = restored.workflow_status(task_id, "default")
        self.assertEqual(["completed", "completed"], [step["status"] for step in snapshot["steps"]])
        self.assertEqual([1, 2], [step["attempt"] for step in snapshot["steps"]])

        # Even a sign-only change must still fail; do not normalize or recompute hashes.
        tampered = copy.deepcopy(original["state"])
        tampered["outputs"]["value"]["negative_zero"] = 0.0
        with restored._connect() as conn:
            conn.execute(
                "UPDATE checkpoints SET state_json=%s::json WHERE task_id=%s AND node='workflow:first'",
                (json.dumps(tampered), task_id),
            )
        with self.assertRaisesRegex(HandoffError, "digest mismatch"):
            flow.run(
                {"value": payload}, task_id=task_id, store=restored, execution_revision=REVISION
            )
        producer.assert_called_once()
        self.assertEqual(2, len(receipts))

    def test_outer_harness_cache_cannot_hide_a_changed_prompt(self):
        store = postgres_store(self)
        task_id = "interrupted-canary"
        store.create(task_id, "demo/repo", 7, {})
        gateway = mock.Mock()
        gateway.route_info.return_value = {"provider": "fixture", "model": "review"}
        gateway.complete.return_value.content = json.dumps(
            {
                "findings": [
                    item.to_dict()
                    for item in LocalRuleReviewer().review(DIFF, parse_unified_diff(DIFF))
                ]
            }
        )

        def harness(prompt):
            reviewer = GatewayReviewer(
                gateway, lambda _task: ModelGovernanceContext("default", "demo/repo"), prompt
            )
            coordinator = MultiAgentCoordinator(
                [reviewer], store=store, checkpoint_revision=REVISION
            )
            return ReviewHarness(store, coordinator, max_steps=16)

        original = harness("candidate prompt")
        with (
            mock.patch.object(store, "succeed", side_effect=RuntimeError("commit interrupted")),
            self.assertRaisesRegex(RuntimeError, "commit interrupted"),
        ):
            original.run(task_id, "demo/repo", 7, DIFF)
        self.assertEqual("", original._ctx.diff)
        checkpoints = store.load_checkpoints(task_id)
        self.assertEqual("completed", checkpoints["executing"]["status"])
        self.assertEqual("completed", checkpoints["reviewing"]["status"])
        self.assertEqual("FAILED", store.get(task_id)["state"])
        gateway.complete.assert_called_once()

        with self.assertRaises(HandoffError):
            harness("stable prompt after rollback").run(task_id, "demo/repo", 7, DIFF)
        self.assertIsNone(store.get(task_id)["report"])
        self.assertEqual(checkpoints, store.load_checkpoints(task_id))
        gateway.complete.assert_called_once()

        load = store.load_checkpoints

        def late_winner(task, node=None):
            # The initial inventory predates the winner, but node reads see its commits.
            return {} if node is None else load(task, node)

        with (
            mock.patch.object(store, "load_checkpoints", side_effect=late_winner),
            self.assertRaises(HandoffError),
        ):
            harness("stable prompt after rollback").run(task_id, "demo/repo", 7, DIFF)
        with self.assertRaises(HandoffError):
            harness("candidate prompt").run(task_id, "demo/repo", 7, DIFF.replace("value", "other"))
        self.assertIsNone(store.get(task_id)["report"])
        self.assertEqual(checkpoints, store.load_checkpoints(task_id))
        gateway.complete.assert_called_once()

        report = original.run(task_id, "demo/repo", 7, DIFF)
        self.assertEqual("SEC-EVAL", report.findings[0].rule_id)
        self.assertEqual("SUCCESS", store.get(task_id)["state"])
        self.assertEqual("", original._ctx.diff)
        gateway.complete.assert_called_once()

    def test_custom_workflow_runs_through_http_and_exposes_only_handoff_metadata(self):
        from examples.custom_review_workflow import load_workflow_factory

        store = postgres_store(self)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            settings = replace(Settings.from_env(), database_url=store.url, skills_dir=directory)
            definition = Path(__file__).resolve().parents[1] / "examples/business-review.json"
            service = ReviewService(settings, workflow_factory=load_workflow_factory(definition))
            self.addCleanup(service.close)
            server = _make_server(replace(settings, port=0), service)
            self.addCleanup(server.server_close)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            self.addCleanup(connection.close)
            connection.request(
                "POST",
                "/v1/reviews",
                json.dumps({"repository": "demo/repo", "pull_request": 7, "diff": DIFF}),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            result = json.loads(response.read())
            self.assertEqual(201, response.status, result)
            connection.request("GET", "/v1/tasks/%s/workflow" % result["task_id"])
            response = connection.getresponse()
            snapshot = json.loads(response.read())
            self.assertEqual(200, response.status, snapshot)
            self.assertEqual("SUCCESS", snapshot["task_state"])
            self.assertEqual("business-review", snapshot["workflow"]["name"])
            self.assertEqual(["completed"] * 8, [step["status"] for step in snapshot["steps"]])
            self.assertEqual("business-policy", snapshot["steps"][-1]["agent_id"])
            self.assertNotIn("eval(value)", json.dumps(snapshot))
            self.assertEqual(service.reviewer.workflow.revision, snapshot["workflow"]["revision"])

    def test_workflow_snapshot_tracks_failure_and_replay_without_payloads(self):
        store = postgres_store(self)
        task_id = "observed-workflow"
        tenant_id = "tenant-a"
        store.create(task_id, "demo/repo", 7, {}, tenant_id=tenant_id)
        self.assertEqual("not_recorded", store.workflow_status(task_id, tenant_id)["availability"])
        self.assertIsNone(store.workflow_status(task_id, "tenant-b"))
        self.assertIsNone(store.workflow_status("missing", tenant_id))
        text = PayloadType("text", 1, lambda value: None)
        first = mock.Mock(side_effect=lambda h: {"value": 2, "secret": h.inputs["secret"]})
        seen = []

        def flaky(handoff):
            seen.append(handoff)
            snapshot = store.workflow_status(task_id, tenant_id)
            self.assertEqual("running", snapshot["steps"][1]["status"])
            self.assertEqual(handoff.idempotency_key, snapshot["steps"][1]["idempotency_key"])
            if len(seen) == 1:
                raise RuntimeError("private-error-text")
            return {"value": 3}

        producer = AgentSpec(
            "producer",
            REVISION,
            {"value": NUMBER, "secret": text},
            {"value": NUMBER, "secret": text},
            first,
        )
        flow = Workflow(
            "observed",
            {"value": NUMBER, "secret": text},
            (
                Step("first", producer, {"value": "$input.value", "secret": "$input.secret"}),
                Step("second", agent(handler=flaky), {"value": "first.value"}),
                Step("third", agent(), {"value": "second.value"}),
            ),
            {"value": "third.value"},
        )

        def run():
            return flow.run(
                {"value": 1, "secret": "private-code-body"},
                task_id=task_id,
                store=store,
                execution_revision=REVISION,
            )

        with self.assertRaises(RuntimeError):
            run()
        self.assertEqual(
            "private-code-body",
            store.load_checkpoints(task_id, "workflow:first")["workflow:first"]["state"]["outputs"][
                "secret"
            ],
        )
        failed = store.workflow_status(task_id, tenant_id)
        self.assertEqual("recorded", failed["availability"])
        self.assertEqual(flow.revision, failed["workflow"]["revision"])
        self.assertEqual(["completed", "failed", "pending"], [s["status"] for s in failed["steps"]])
        self.assertEqual(["second"], failed["steps"][2]["blocked_by"])
        self.assertEqual([1, 1, 0], [s["attempt"] for s in failed["steps"]])
        self.assertTrue(all(step["started_at"] for step in failed["steps"][:2]))
        self.assertTrue(all(type(step["duration_ms"]) is int for step in failed["steps"][:2]))
        self.assertIsNone(failed["steps"][2]["duration_ms"])
        self.assertEqual({"value": 4}, run())
        completed = store.workflow_status(task_id, tenant_id)
        self.assertEqual(["completed"] * 3, [s["status"] for s in completed["steps"]])
        self.assertEqual([1, 2, 1], [s["attempt"] for s in completed["steps"]])
        self.assertTrue(all(step["started_at"] for step in completed["steps"]))
        self.assertTrue(all(type(step["duration_ms"]) is int for step in completed["steps"]))
        self.assertEqual(seen[0].idempotency_key, seen[1].idempotency_key)
        first.assert_called_once()
        for snapshot in (failed, completed):
            encoded = json.dumps(snapshot)
            self.assertNotIn("private-code-body", encoded)
            self.assertNotIn("private-error-text", encoded)
        store.cancel(task_id, TraceEvent(1, TaskState.CANCELLED, "cancelled", utc_now()))
        cutoff = "2999-01-01T00:00:00+00:00"
        pruned_at = utc_now()
        store.prune_operational_history(cutoff, cutoff, 10, pruned_at)
        pruned = store.workflow_status(task_id, tenant_id)
        self.assertEqual("pruned", pruned["availability"])
        self.assertEqual(pruned_at, pruned["artifacts_pruned_at"])
        self.assertEqual([], pruned["steps"])

    def test_real_harness_persists_each_agent_and_replays_without_reexecution(self):
        store = postgres_store(self)
        store.create("workflow-task", "demo/repo", 7, {"source": "test"})
        reviewer = LocalRuleReviewer()
        reviewer.review = mock.Mock(wraps=reviewer.review)
        coordinator = MultiAgentCoordinator([reviewer], store=store, checkpoint_revision=REVISION)
        report = ReviewHarness(store, coordinator).run("workflow-task", "demo/repo", 7, DIFF)
        self.assertEqual("high", report.risk)
        checkpoints = store.load_checkpoints("workflow-task")
        self.assertEqual("completed", checkpoints["workflow:verify"]["status"])
        self.assertEqual(11, len(checkpoints))
        ReviewHarness(store, coordinator).run("workflow-task", "demo/repo", 7, DIFF)
        reviewer.review.assert_called_once()
