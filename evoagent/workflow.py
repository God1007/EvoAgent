"""Trusted, typed DAG composition with durable, receiver-validated handoffs.

No plugin discovery or agent-to-agent RPC: deployment code supplies handlers;
the runner alone routes data and commits outputs before releasing dependants.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from types import MappingProxyType
from typing import Any, ClassVar

from .errors import safe_exception_summary
from .ports import ReviewExecutionStorePort

_TOKEN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_WORKFLOW_STEPS = 64
MAX_HANDOFF_BYTES = 8 * 1024 * 1024


class HandoffError(ValueError):
    """A deterministic contract/revision failure; retrying cannot repair it."""


class BudgetExceeded(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    pass


def _token(value: str) -> None:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("workflow identifiers must be bounded alphanumeric tokens")


def _json(value: Any) -> bytes:
    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError("non-string JSON key")
            for key, nested in item.items():
                check(key)
                check(nested)
        elif isinstance(item, list):
            for nested in item:
                check(nested)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError("non-JSON value")
        elif isinstance(item, str) and "\x00" in item:
            raise ValueError("NUL is not supported by JSONB")

    try:
        check(value)
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
        ).encode()
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise HandoffError("handoff must contain finite, serializable JSON data") from None
    if len(encoded) > MAX_HANDOFF_BYTES:
        raise HandoffError("handoff exceeds the payload size limit")
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


@dataclass(frozen=True)
class PayloadType:
    """A wire contract; pure validators raise ValueError without modifying data."""

    name: str
    version: int
    validate: Callable[[Any], Any]

    def __post_init__(self) -> None:
        _token(self.name)
        if type(self.version) is not int or self.version < 1 or not callable(self.validate):
            raise ValueError("payload type requires a positive version and validator")

    @property
    def key(self) -> str:
        return "%s@%d" % (self.name, self.version)


def _ports(ports: Mapping[str, PayloadType]) -> Mapping[str, PayloadType]:
    if len(ports) > MAX_WORKFLOW_STEPS:
        raise ValueError("too many workflow ports")
    for name, contract in ports.items():
        _token(name)
        if not isinstance(contract, PayloadType):
            raise TypeError("agent ports must declare PayloadType contracts")
    return MappingProxyType(dict(ports))


def _validate(ports: Mapping[str, PayloadType], values: Any) -> dict[str, Any]:
    if not isinstance(values, dict) or values.keys() != ports.keys():
        raise HandoffError("handoff ports do not match the declared contract")
    # Serialization also isolates nested mutable state between agents and branches.
    encoded = _json(values)
    detached = json.loads(encoded)
    for name, contract in ports.items():
        try:
            contract.validate(detached[name])
        except Exception:
            raise HandoffError("invalid handoff payload: %s (%s)" % (name, contract.key)) from None
    if _json(detached) != encoded:
        raise HandoffError("handoff validators must not modify payloads")
    return detached


@dataclass(frozen=True)
class Handoff:
    protocol_version: ClassVar[int] = 1
    task_id: str
    workflow_revision: str
    step_id: str
    agent_id: str
    agent_revision: str
    idempotency_key: str
    attempt: int
    generation: int | None
    deadline: float
    inputs: Mapping[str, Any]
    sources: Mapping[str, str]
    _check_active: Callable[[], None] = field(repr=False, compare=False)

    def check_active(self) -> None:
        """Check cancellation, generation and deadline before expensive/external work."""
        self._check_active()


@dataclass(frozen=True)
class AgentSpec:
    """An installed agent, pinned to its immutable implementation/config digest."""

    agent_id: str
    revision: str
    inputs: Mapping[str, PayloadType]
    outputs: Mapping[str, PayloadType]
    run: Callable[[Handoff], dict[str, Any]]

    def __post_init__(self) -> None:
        _token(self.agent_id)
        if not isinstance(self.revision, str) or not _DIGEST.fullmatch(self.revision):
            raise ValueError("agent revision must be an immutable SHA-256 digest")
        if not self.outputs or not callable(self.run):
            raise ValueError("agent requires outputs and a callable handler")
        object.__setattr__(self, "inputs", _ports(self.inputs))
        object.__setattr__(self, "outputs", _ports(self.outputs))


@dataclass(frozen=True)
class Step:
    """Wire each input port to '$input.port' or 'upstream_step.output_port'."""

    step_id: str
    agent: AgentSpec
    sources: Mapping[str, str]

    def __post_init__(self) -> None:
        _token(self.step_id)
        if not isinstance(self.agent, AgentSpec) or self.sources.keys() != self.agent.inputs.keys():
            raise ValueError("step wiring must supply exactly the agent's input ports")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


@dataclass(frozen=True, init=False, repr=False, eq=False)
class Workflow:
    """A bounded DAG; independent branches run serially, joins require every input.

    ponytail: serial scheduling avoids another thread pool; add parallel scheduling
    only if measured agent latency warrants it. Specialist agents already fan out.
    """

    name: str
    inputs: Mapping[str, PayloadType]
    steps: tuple[Step, ...]
    outputs: Mapping[str, str]
    output_types: Mapping[str, PayloadType] = field(init=False)
    revision: str = field(init=False)
    _order: tuple[Step, ...] = field(init=False)

    def __init__(
        self,
        name: str,
        inputs: Mapping[str, PayloadType],
        steps: Sequence[Step],
        outputs: Mapping[str, str],
    ):
        _token(name)
        if not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
            raise ValueError("workflow requires 1 to %d steps" % MAX_WORKFLOW_STEPS)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "inputs", _ports(inputs))
        object.__setattr__(self, "steps", tuple(steps))
        object.__setattr__(self, "outputs", MappingProxyType(dict(outputs)))
        by_id = {step.step_id: step for step in self.steps}
        if len(by_id) != len(self.steps) or not self.outputs:
            raise ValueError("workflow requires unique step ids and declared outputs")
        ports = {"$input": self.inputs, **{key: step.agent.outputs for key, step in by_id.items()}}

        def source(ref: str) -> tuple[str, PayloadType]:
            if not isinstance(ref, str) or ref.count(".") != 1:
                raise ValueError("invalid workflow source reference")
            node, port = ref.split(".")
            if node not in ports or port not in ports[node]:
                raise ValueError("workflow source does not exist: %s" % ref)
            return node, ports[node][port]

        dependencies: dict[str, set[str]] = {}
        for step in self.steps:
            dependencies[step.step_id] = set()
            for port, ref in step.sources.items():
                node, contract = source(ref)
                if contract.key != step.agent.inputs[port].key:
                    raise ValueError(
                        "incompatible handoff contract at %s.%s" % (step.step_id, port)
                    )
                if node != "$input":
                    dependencies[step.step_id].add(node)
        object.__setattr__(
            self, "output_types", _ports({key: source(ref)[1] for key, ref in self.outputs.items()})
        )
        try:
            object.__setattr__(
                self,
                "_order",
                tuple(
                    by_id[key]
                    for key in TopologicalSorter(
                        {key: sorted(parents) for key, parents in dependencies.items()}
                    ).static_order()
                ),
            )
        except CycleError:
            raise ValueError("workflow must be acyclic") from None
        object.__setattr__(self, "revision", _digest(self.describe()))

    @classmethod
    def from_dict(
        cls, definition: dict, agents: Mapping[str, AgentSpec], inputs: Mapping[str, PayloadType]
    ) -> Workflow:
        """Load wiring only; configuration cannot import or execute Python modules."""
        if not isinstance(definition, dict) or definition.keys() != {"name", "steps", "outputs"}:
            raise ValueError("workflow definition requires name, steps and outputs")
        raw_steps = definition["steps"]
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_WORKFLOW_STEPS:
            raise ValueError("invalid workflow step count")
        steps = []
        for raw in raw_steps:
            if not isinstance(raw, dict) or raw.keys() != {"id", "agent", "sources"}:
                raise ValueError("step definition requires id, agent and sources")
            if not isinstance(raw["agent"], str) or raw["agent"] not in agents:
                raise ValueError("workflow references an agent not registered by the deployment")
            if not isinstance(raw["sources"], dict):
                raise ValueError("step sources must be an object")
            steps.append(Step(raw["id"], agents[raw["agent"]], raw["sources"]))
        if not isinstance(definition["outputs"], dict):
            raise ValueError("workflow outputs must be an object")
        return cls(definition["name"], inputs, steps, definition["outputs"])

    def describe(self) -> dict[str, Any]:
        return {
            "protocol_version": 1,
            "name": self.name,
            "inputs": {key: value.key for key, value in self.inputs.items()},
            "steps": [
                {
                    "id": step.step_id,
                    "agent": step.agent.agent_id,
                    "revision": step.agent.revision,
                    "inputs": {key: value.key for key, value in step.agent.inputs.items()},
                    "outputs": {key: value.key for key, value in step.agent.outputs.items()},
                    "sources": dict(step.sources),
                }
                for step in self.steps
            ],
            "outputs": dict(self.outputs),
        }

    def execution_snapshot(self, inputs: dict[str, Any], execution_revision: str) -> dict[str, Any]:
        """The shared resume binding, including callers that cache the workflow result."""
        return {
            "protocol_version": 1,
            "workflow_revision": self.revision,
            "execution_revision": execution_revision,
            "workflow_input_sha256": _digest(inputs),
            "definition": self.describe(),
        }

    def run(
        self,
        inputs: dict[str, Any],
        *,
        task_id: str = "",
        store: ReviewExecutionStorePort | None = None,
        generation: int | None = None,
        execution_revision: str = "",
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("workflow timeout must be positive and finite")
        if store is not None and (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(execution_revision, str)
            or not _DIGEST.fullmatch(execution_revision)
        ):
            raise ValueError("durable workflow requires task id and pinned execution revision")
        deadline = time.monotonic() + timeout_seconds

        def active() -> None:
            if store is not None and (
                store.is_cancelled(task_id)
                or (
                    generation is not None
                    and not store.review_admission_active(task_id, generation)
                )
            ):
                raise TaskCancelled("Task was cancelled or admission generation was replaced")
            if time.monotonic() >= deadline:
                raise BudgetExceeded("workflow execution budget exceeded")

        def checkpoint(node: str) -> dict[str, Any]:
            return store.load_checkpoints(task_id, node).get(node, {}) if store is not None else {}

        def save(
            node: str, state: dict, attempt: int = 1, error: str = "", *, status: str = "completed"
        ) -> None:
            active()
            if (
                store is not None
                and store.save_checkpoint(
                    task_id,
                    node,
                    state,
                    "failed" if error else status,
                    attempt,
                    error,
                    generation,
                )
                is False
            ):
                raise TaskCancelled("Task was cancelled or admission generation was replaced")

        values = {"$input": _validate(self.inputs, inputs)}
        snapshot = self.execution_snapshot(values["$input"], execution_revision)
        manifest = {key: value for key, value in snapshot.items() if key != "definition"}
        active()
        if store is not None:
            save("workflow", snapshot)
            saved_manifest = checkpoint("workflow")
            if (
                saved_manifest.get("status") != "completed"
                or saved_manifest.get("state") != snapshot
            ):
                raise HandoffError("workflow revision or original input changed; start a new task")

        def resolve(refs: Mapping[str, str]) -> dict[str, Any]:
            return {key: values[ref.split(".")[0]][ref.split(".")[1]] for key, ref in refs.items()}

        for step in self._order:
            active()
            incoming = resolve(step.sources)
            identity = {
                **manifest,
                "task_id": task_id,
                "step_id": step.step_id,
                "agent_id": step.agent.agent_id,
                "agent_revision": step.agent.revision,
                "sources": dict(step.sources),
            }
            node = "workflow:%s" % step.step_id
            existing = checkpoint(node)
            attempt = int(existing.get("attempt", 0)) + 1
            envelope: dict[str, Any] = {"handoff": identity, "generation": generation}
            try:
                identity["input_sha256"] = _digest(incoming)
                key = _digest(identity)
                envelope["idempotency_key"] = key
                incoming = _validate(step.agent.inputs, incoming)
            except HandoffError as exc:
                save(node, envelope, attempt, safe_exception_summary(exc))
                raise

            def accept(
                record: dict,
                current_step: Step = step,
                expected: dict = identity,
                expected_key: str = key,
            ) -> dict[str, Any]:
                state = record.get("state", {})
                if (
                    record.get("status") != "completed"
                    or not isinstance(state, dict)
                    or state.get("handoff") != expected
                    or state.get("idempotency_key") != expected_key
                ):
                    raise HandoffError("checkpoint does not match the incoming handoff")
                output = _validate(current_step.agent.outputs, state.get("outputs"))
                if _digest(output) != state.get("output_sha256"):
                    raise HandoffError("checkpoint output digest mismatch")
                return output

            if existing.get("status") == "completed":
                values[step.step_id] = accept(existing)
                continue
            handoff = Handoff(
                task_id,
                self.revision,
                step.step_id,
                step.agent.agent_id,
                step.agent.revision,
                key,
                attempt,
                generation,
                deadline,
                MappingProxyType(incoming),
                step.sources,
                active,
            )
            try:
                if store is not None:
                    save(node, envelope, attempt, status="running")
                    dispatched = checkpoint(node)
                    if dispatched.get("status") == "completed":
                        values[step.step_id] = accept(dispatched)
                        continue
                output = _validate(step.agent.outputs, step.agent.run(handoff))
                save(
                    node,
                    {
                        **envelope,
                        "outputs": output,
                        "output_sha256": _digest(output),
                    },
                    attempt,
                )
            except (TaskCancelled, BudgetExceeded):
                raise
            except Exception as exc:
                if store is not None:
                    save(node, envelope, attempt, safe_exception_summary(exc))
                    winner = checkpoint(node)
                    if winner.get("status") == "completed":
                        values[step.step_id] = accept(winner)
                        continue
                raise
            # First-write-wins: downstream consumes the committed winner, never a late local result.
            values[step.step_id] = accept(checkpoint(node)) if store is not None else output
        active()
        return _validate(self.output_types, resolve(self.outputs))
