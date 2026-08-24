"""Structured multi-agent review protocol.

The coordinator is intentionally explicit: a planner creates assignments,
specialists produce evidence, a critic challenges each claim, a test agent
checks reproducibility, a synthesizer resolves conflicts, a fix agent checks
remediation quality, and a verifier makes the final release decision.
"""

import hashlib
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import asdict, dataclass
from typing import Any, TypedDict, cast

from .diff_parser import ParsedDiff
from .errors import safe_exception_summary
from .metrics import metrics
from .models import Finding, Severity
from .ports import AgentMessageStorePort
from .reviewer import (
    MAX_REVIEWER_FINDINGS,
    MAX_REVIEWER_NAME_CHARS,
    Reviewer,
    valid_reviewer_name,
)

# ponytail: fixed graph ceiling; raise only when 64 active specialists prove insufficient.
MAX_REVIEW_AGENTS = 64
MAX_REVIEW_AGENT_NAME_CHARS = MAX_REVIEWER_NAME_CHARS


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    kind: str
    content: dict[str, Any]
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewAssignment:
    agent: str
    objective: str
    files: list[str]
    risk_domains: list[str]


@dataclass
class ReviewPlan:
    languages: list[str]
    changed_files: list[str]
    risk_level: str
    assignments: list[ReviewAssignment]

    def to_dict(self) -> dict:
        return {
            "languages": self.languages,
            "changed_files": self.changed_files,
            "risk_level": self.risk_level,
            "assignments": [asdict(item) for item in self.assignments],
        }


@dataclass
class Critique:
    finding_key: str
    accepted: bool
    objections: list[str]
    confidence_adjustment: float


@dataclass
class Reproduction:
    finding_key: str
    reproducible: bool
    method: str
    evidence: str


class CollaborationState(TypedDict, total=False):
    diff: str
    parsed: ParsedDiff
    task_id: str
    admission_generation: int | None
    plan: ReviewPlan
    specialist_findings: list[Finding]
    critiques: dict[str, Critique]
    reproductions: dict[str, Reproduction]
    synthesized: list[Finding]
    fix_ready: dict[str, bool]
    verified: list[Finding]


def finding_key(finding: Finding) -> str:
    """Per-run correlation key that distinguishes co-located claims.

    Confidence is excluded because synthesis adjusts it before later nodes look
    the key up again. Exact duplicate claims intentionally share a key. For a
    line-independent identity stable across PR revisions, use ``fingerprint``.
    """
    claim = asdict(finding)
    claim.pop("confidence")
    raw = json.dumps(claim, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class FilteredAgent(Reviewer):
    def __init__(self, name: str, reviewer: Reviewer, prefixes: tuple):
        self.name = name
        self.reviewer = reviewer
        self.prefixes = prefixes

    def review(self, diff: str, parsed: ParsedDiff) -> list[Finding]:
        return self._filter(self.reviewer.review(diff, parsed))

    def review_with_context(
        self,
        task_id: str,
        diff: str,
        parsed: ParsedDiff,
        admission_generation: int | None = None,
    ) -> list[Finding]:
        contextual = getattr(self.reviewer, "review_with_context", None)
        findings = (
            contextual(task_id, diff, parsed, admission_generation)
            if callable(contextual)
            else self.reviewer.review(diff, parsed)
        )
        return self._filter(findings)

    def _filter(self, findings: list[Finding]) -> list[Finding]:
        return [item for item in findings if item.rule_id.startswith(self.prefixes)]


class PlannerAgent:
    name = "planner-agent"

    def plan(self, parsed: ParsedDiff, specialists: list[Reviewer]) -> ReviewPlan:
        extensions = {path.rsplit(".", 1)[-1].lower() for path in parsed.files if "." in path}
        languages = sorted(
            {
                "python"
                if ext == "py"
                else "javascript"
                if ext in {"js", "jsx", "ts", "tsx"}
                else "configuration"
                if ext in {"yml", "yaml", "json", "toml"}
                else ext
                for ext in extensions
            }
        )
        sensitive = any(
            token in path.lower()
            for path in parsed.files
            for token in ("auth", "security", "payment", "permission", "token", "migration")
        )
        domains = ["correctness", "regression"]
        if sensitive:
            domains.extend(["security", "authorization"])
        assignments = [
            ReviewAssignment(
                agent=agent.name,
                objective="Find actionable defects introduced by this change and provide evidence.",
                files=list(parsed.files),
                risk_domains=list(domains),
            )
            for agent in specialists
        ]
        return ReviewPlan(
            languages=languages or ["unknown"],
            changed_files=list(parsed.files),
            risk_level="high" if sensitive or len(parsed.files) > 10 else "normal",
            assignments=assignments,
        )


class CriticAgent:
    name = "critic-agent"

    def challenge(self, finding: Finding, parsed: ParsedDiff) -> Critique:
        objections = []
        valid_locations = {(line.path, line.line) for line in parsed.added_lines}
        if (finding.path, finding.line) not in valid_locations:
            objections.append("location is not an added line")
        source_line = next(
            (
                line.content
                for line in parsed.added_lines
                if line.path == finding.path and line.line == finding.line
            ),
            "",
        )
        if not finding.evidence.strip():
            objections.append("quoted evidence is required")
        elif finding.evidence.strip() not in source_line.strip():
            objections.append("quoted evidence does not match the changed line")
        if not finding.title.strip():
            objections.append("title is required")
        if len(finding.explanation.strip()) < 12:
            objections.append("explanation is not specific enough")
        if len(finding.fix.strip()) < 8:
            objections.append("remediation is not actionable")
        if len(finding.test.strip()) < 8:
            objections.append("test strategy is not actionable")
        adjustment = -0.35 if objections else 0.05
        return Critique(finding_key(finding), not objections, objections, adjustment)


class TestAgent:
    name = "test-agent"

    def reproduce(self, finding: Finding, parsed: ParsedDiff) -> Reproduction:
        line = next(
            (
                item.content
                for item in parsed.added_lines
                if item.path == finding.path and item.line == finding.line
            ),
            "",
        )
        signatures = {
            "SEC-EVAL": ("eval(" in line or "exec(" in line),
            "SEC-SUBPROCESS-SHELL": "shell=True" in line.replace(" ", ""),
            "SEC-HARDCODED-SECRET": any(
                token in line.lower() for token in ("password", "secret", "token", "api_key")
            ),
            "SEC-SQL-CONCAT": any(token in line for token in ("execute(", "query(")),
            "REL-DEBUG-PRINT": "print(" in line or "console.log(" in line,
            "REL-EMPTY-EXCEPT": "except" in line,
        }
        reproducible = signatures.get(finding.rule_id, bool(line and finding.evidence))
        return Reproduction(
            finding_key(finding),
            reproducible,
            "static changed-line reproduction",
            line.strip()[:240] if reproducible else "No matching changed-line evidence.",
        )


class SynthesizerAgent:
    name = "synthesizer-agent"

    def synthesize(
        self,
        findings: list[Finding],
        critiques: dict[str, Critique],
        reproductions: dict[str, Reproduction],
    ) -> list[Finding]:
        merged: dict[tuple, Finding] = {}
        for finding in findings:
            key = finding_key(finding)
            critique = critiques[key]
            reproduction = reproductions[key]
            adjusted = max(0.0, min(1.0, finding.confidence + critique.confidence_adjustment))
            if not critique.accepted:
                continue
            if (
                finding.severity in {Severity.CRITICAL, Severity.HIGH}
                and not reproduction.reproducible
            ):
                continue
            finding.confidence = adjusted
            identity = (finding.path, finding.line, finding.rule_id)
            current = merged.get(identity)
            if current is None or (finding.confidence, key) > (
                current.confidence,
                finding_key(current),
            ):
                merged[identity] = finding
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(
            merged.values(),
            key=lambda item: (order[item.severity], item.path, item.line, item.rule_id),
        )


class FixAgent:
    name = "fix-agent"

    def assess(self, finding: Finding) -> bool:
        dangerous = ("disable validation", "ignore error", "catch all")
        text = finding.fix.lower()
        return bool(finding.fix and not any(item in text for item in dangerous))


class VerifierAgent:
    name = "verifier-agent"

    def verify(
        self,
        finding: Finding,
        reproduction: Reproduction,
        fix_ready: bool,
    ) -> bool:
        if not fix_ready or finding.confidence < 0.55:
            return False
        if finding.severity in {Severity.CRITICAL, Severity.HIGH}:
            return reproduction.reproducible
        return True


class MultiAgentCoordinator(Reviewer):
    """Planner/specialist/critic/test/synthesis/fix/verifier collaboration graph."""

    name = "multi-agent-collaboration"

    def __init__(
        self,
        agents: list[Reviewer],
        max_workers: int = 4,
        store: AgentMessageStorePort | None = None,
        timeout_seconds: float = 120,
    ):
        if not agents:
            raise ValueError("at least one review agent is required")
        if len(agents) > MAX_REVIEW_AGENTS:
            raise ValueError("review graph accepts at most %d agents" % MAX_REVIEW_AGENTS)
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("review agent workers must be a positive integer")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("review agent timeout must be positive and finite")
        self.planner = PlannerAgent()
        self.critic = CriticAgent()
        self.test_agent = TestAgent()
        self.synthesizer = SynthesizerAgent()
        self.fix_agent = FixAgent()
        self.verifier = VerifierAgent()
        agent_names = [getattr(agent, "name", None) for agent in agents]
        if any(not valid_reviewer_name(name) for name in agent_names):
            raise ValueError(
                "review agent names must be printable, whitespace-free strings of at most %d characters"
                % MAX_REVIEW_AGENT_NAME_CHARS
            )
        names = set(agent_names)
        if len(names) != len(agents):
            raise ValueError("review agent names must be unique")
        if names & {
            self.planner.name,
            self.critic.name,
            self.test_agent.name,
            self.synthesizer.name,
            self.fix_agent.name,
            self.verifier.name,
        }:
            raise ValueError("review agent names must not collide with coordinator nodes")
        self.agents = agents
        self.max_workers = min(max_workers, len(agents))
        self._worker_slots = threading.BoundedSemaphore(self.max_workers)
        self.store = store
        self.timeout_seconds = timeout_seconds

    def review(self, diff: str, parsed: ParsedDiff) -> list[Finding]:
        return self.review_with_context("", diff, parsed)

    def review_with_context(
        self,
        task_id: str,
        diff: str,
        parsed: ParsedDiff,
        admission_generation: int | None = None,
    ) -> list[Finding]:
        state: CollaborationState = {
            "task_id": task_id,
            "diff": diff,
            "parsed": parsed,
            "admission_generation": admission_generation,
        }
        result: dict[str, Any] = dict(state)
        for node in (
            self._plan_node,
            self._specialist_node,
            self._critic_node,
            self._test_node,
            self._synthesize_node,
            self._fix_node,
            self._verify_node,
        ):
            result.update(node(cast(CollaborationState, result)))
        return result["verified"]

    def _emit(
        self,
        state: CollaborationState,
        sender: str,
        recipient: str,
        kind: str,
        content: dict[str, Any],
        correlation_id: str = "",
    ) -> None:
        message = AgentMessage(sender, recipient, kind, content, correlation_id)
        if self.store is not None and state.get("task_id"):
            if (
                self.store.record_agent_message(
                    state["task_id"], message.to_dict(), state.get("admission_generation")
                )
                is False
            ):
                raise RuntimeError("agent message persistence was rejected")

    def _plan_node(self, state: CollaborationState) -> dict[str, Any]:
        plan = self.planner.plan(state["parsed"], self.agents)
        self._emit(state, self.planner.name, "specialists", "review_plan", plan.to_dict())
        return {"plan": plan}

    def _specialist_node(self, state: CollaborationState) -> dict[str, Any]:
        findings: list[Finding] = []
        evidence = []
        failures = []

        def invoke(agent: Reviewer) -> list[Finding]:
            contextual = getattr(agent, "review_with_context", None)
            if callable(contextual):
                return contextual(
                    state.get("task_id", ""),
                    state["diff"],
                    state["parsed"],
                    state.get("admission_generation"),
                )
            return agent.review(state["diff"], state["parsed"])

        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        futures = {}
        deadline = time.monotonic() + self.timeout_seconds
        try:
            for agent in self.agents:
                if not self._worker_slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                    raise FuturesTimeoutError
                try:
                    future = pool.submit(invoke, agent)
                except Exception:
                    self._worker_slots.release()
                    raise
                future.add_done_callback(lambda _future: self._worker_slots.release())
                futures[future] = agent
            for future in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                agent = futures[future]
                try:
                    output = future.result()
                    if not isinstance(output, list):
                        raise RuntimeError("review agent returned invalid findings")
                    if len(output) > MAX_REVIEWER_FINDINGS:
                        raise RuntimeError("review agent returned too many findings")
                    output = [Finding.from_dict(asdict(item)) for item in output]
                    findings.extend(output)
                    evidence.append((agent, output))
                except Exception as exc:
                    summary = safe_exception_summary(exc, "review agent failed")
                    failures.append("%s: %s" % (agent.name, summary))
                    self._emit(
                        state,
                        agent.name,
                        self.planner.name,
                        "agent_failure",
                        {"error": summary},
                    )
        except FuturesTimeoutError:
            metrics.inc("review_agent_budget_timeouts_total")
            raise RuntimeError("review agents exceeded the execution budget") from None
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
        if failures:
            scope = (
                "all review agents failed"
                if len(failures) == len(self.agents)
                else "review agents failed"
            )
            raise RuntimeError(scope + ": " + "; ".join(failures))
        if len(findings) > MAX_REVIEWER_FINDINGS:
            metrics.inc("review_agent_output_limit_rejections_total")
            raise RuntimeError("review agents returned too many findings in aggregate")
        for agent, output in evidence:
            self._emit(
                state,
                agent.name,
                self.critic.name,
                "specialist_evidence",
                {"findings": [item.to_dict() for item in output]},
            )
        return {"specialist_findings": findings}

    def _critic_node(self, state: CollaborationState) -> dict[str, Any]:
        critiques = {}
        for finding in state["specialist_findings"]:
            critique = self.critic.challenge(finding, state["parsed"])
            critiques[critique.finding_key] = critique
            self._emit(
                state,
                self.critic.name,
                self.test_agent.name,
                "critique",
                asdict(critique),
                critique.finding_key,
            )
        return {"critiques": critiques}

    def _test_node(self, state: CollaborationState) -> dict[str, Any]:
        reproductions = {}
        for finding in state["specialist_findings"]:
            reproduction = self.test_agent.reproduce(finding, state["parsed"])
            reproductions[reproduction.finding_key] = reproduction
            self._emit(
                state,
                self.test_agent.name,
                self.synthesizer.name,
                "reproduction",
                asdict(reproduction),
                reproduction.finding_key,
            )
        return {"reproductions": reproductions}

    def _synthesize_node(self, state: CollaborationState) -> dict[str, Any]:
        findings = self.synthesizer.synthesize(
            state["specialist_findings"], state["critiques"], state["reproductions"]
        )
        self._emit(
            state,
            self.synthesizer.name,
            self.fix_agent.name,
            "arbitration",
            {"accepted_findings": [item.to_dict() for item in findings]},
        )
        return {"synthesized": findings}

    def _fix_node(self, state: CollaborationState) -> dict[str, Any]:
        fix_ready = {
            finding_key(item): self.fix_agent.assess(item) for item in state["synthesized"]
        }
        self._emit(
            state,
            self.fix_agent.name,
            self.verifier.name,
            "fix_assessment",
            {"decisions": fix_ready},
        )
        return {"fix_ready": fix_ready}

    def _verify_node(self, state: CollaborationState) -> dict[str, Any]:
        verified = [
            item
            for item in state["synthesized"]
            if self.verifier.verify(
                item,
                state["reproductions"][finding_key(item)],
                state["fix_ready"][finding_key(item)],
            )
        ]
        self._emit(
            state,
            self.verifier.name,
            "review-report",
            "verification_decision",
            {"approved_findings": [item.to_dict() for item in verified]},
        )
        return {"verified": verified}
