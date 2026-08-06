"""Structured multi-agent review protocol.

The coordinator is intentionally explicit: a planner creates assignments,
specialists produce evidence, a critic challenges each claim, a test agent
checks reproducibility, a synthesizer resolves conflicts, a fix agent checks
remediation quality, and a verifier makes the final release decision.
"""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, TypedDict

from .diff_parser import ParsedDiff
from .models import Finding, Severity
from .reviewer import Reviewer


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    kind: str
    content: Dict[str, Any]
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewAssignment:
    agent: str
    objective: str
    files: List[str]
    risk_domains: List[str]


@dataclass
class ReviewPlan:
    languages: List[str]
    changed_files: List[str]
    risk_level: str
    assignments: List[ReviewAssignment]

    def to_dict(self) -> dict:
        return {
            "languages": self.languages, "changed_files": self.changed_files,
            "risk_level": self.risk_level,
            "assignments": [asdict(item) for item in self.assignments],
        }


@dataclass
class Critique:
    finding_key: str
    accepted: bool
    objections: List[str]
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
    plan: ReviewPlan
    specialist_findings: List[Finding]
    critiques: Dict[str, Critique]
    reproductions: Dict[str, Reproduction]
    synthesized: List[Finding]
    fix_ready: Dict[str, bool]
    verified: List[Finding]


def finding_key(finding: Finding) -> str:
    raw = "%s:%s:%s" % (finding.path, finding.line, finding.rule_id)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class FilteredAgent(Reviewer):
    def __init__(self, name: str, reviewer: Reviewer, prefixes: tuple):
        self.name = name
        self.reviewer = reviewer
        self.prefixes = prefixes

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        return [
            item for item in self.reviewer.review(diff, parsed)
            if item.rule_id.startswith(self.prefixes)
        ]


class PlannerAgent:
    name = "planner-agent"

    def plan(self, parsed: ParsedDiff, specialists: List[Reviewer]) -> ReviewPlan:
        extensions = {path.rsplit(".", 1)[-1].lower() for path in parsed.files if "." in path}
        languages = sorted({
            "python" if ext == "py" else
            "javascript" if ext in {"js", "jsx", "ts", "tsx"} else
            "configuration" if ext in {"yml", "yaml", "json", "toml"} else ext
            for ext in extensions
        })
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
            languages=languages or ["unknown"], changed_files=list(parsed.files),
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
            (line.content for line in parsed.added_lines
             if line.path == finding.path and line.line == finding.line), ""
        )
        if finding.evidence and finding.evidence.strip() not in source_line.strip():
            objections.append("quoted evidence does not match the changed line")
        if len(finding.explanation.strip()) < 12:
            objections.append("explanation is not specific enough")
        if len(finding.fix.strip()) < 8:
            objections.append("remediation is not actionable")
        if len(finding.test.strip()) < 8:
            objections.append("test strategy is not actionable")
        adjustment = -.35 if objections else .05
        return Critique(finding_key(finding), not objections, objections, adjustment)


class TestAgent:
    name = "test-agent"

    def reproduce(self, finding: Finding, parsed: ParsedDiff) -> Reproduction:
        line = next(
            (item.content for item in parsed.added_lines
             if item.path == finding.path and item.line == finding.line), ""
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
            finding_key(finding), reproducible,
            "static changed-line reproduction",
            line.strip()[:240] if reproducible else "No matching changed-line evidence.",
        )


class SynthesizerAgent:
    name = "synthesizer-agent"

    def synthesize(
        self, findings: List[Finding], critiques: Dict[str, Critique],
        reproductions: Dict[str, Reproduction],
    ) -> List[Finding]:
        merged: Dict[tuple, Finding] = {}
        for finding in findings:
            key = finding_key(finding)
            critique = critiques[key]
            reproduction = reproductions[key]
            adjusted = max(0.0, min(1.0, finding.confidence + critique.confidence_adjustment))
            if not critique.accepted:
                continue
            if finding.severity in {Severity.CRITICAL, Severity.HIGH} and not reproduction.reproducible:
                continue
            finding.confidence = adjusted
            identity = (finding.path, finding.line, finding.rule_id)
            current = merged.get(identity)
            if current is None or finding.confidence > current.confidence:
                merged[identity] = finding
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(merged.values(), key=lambda item: (order[item.severity], item.path, item.line))


class FixAgent:
    name = "fix-agent"

    def assess(self, finding: Finding) -> bool:
        dangerous = ("disable validation", "ignore error", "catch all")
        text = finding.fix.lower()
        return bool(finding.fix and not any(item in text for item in dangerous))


class VerifierAgent:
    name = "verifier-agent"

    def verify(
        self, finding: Finding, reproduction: Reproduction, fix_ready: bool,
    ) -> bool:
        if not fix_ready or finding.confidence < .55:
            return False
        if finding.severity in {Severity.CRITICAL, Severity.HIGH}:
            return reproduction.reproducible
        return True


class MultiAgentCoordinator(Reviewer):
    """Planner/specialist/critic/test/synthesis/fix/verifier collaboration graph."""
    name = "multi-agent-collaboration"

    def __init__(
        self, agents: List[Reviewer], max_workers: int = 4, store=None,
    ):
        self.agents = agents
        self.max_workers = max_workers
        self.store = store
        self.planner = PlannerAgent()
        self.critic = CriticAgent()
        self.test_agent = TestAgent()
        self.synthesizer = SynthesizerAgent()
        self.fix_agent = FixAgent()
        self.verifier = VerifierAgent()
        self.graph = self._build_graph()

    def _build_graph(self):
        try:
            from langgraph.graph import END, START, StateGraph
            graph = StateGraph(CollaborationState)
            graph.add_node("planner", self._plan_node)
            graph.add_node("specialists", self._specialist_node)
            graph.add_node("critic", self._critic_node)
            graph.add_node("test", self._test_node)
            graph.add_node("synthesizer", self._synthesize_node)
            graph.add_node("fix", self._fix_node)
            graph.add_node("verifier", self._verify_node)
            graph.add_edge(START, "planner")
            graph.add_edge("planner", "specialists")
            graph.add_edge("specialists", "critic")
            graph.add_edge("critic", "test")
            graph.add_edge("test", "synthesizer")
            graph.add_edge("synthesizer", "fix")
            graph.add_edge("fix", "verifier")
            graph.add_edge("verifier", END)
            return graph.compile()
        except ImportError:
            return None

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        return self.review_with_context("", diff, parsed)

    def review_with_context(
        self, task_id: str, diff: str, parsed: ParsedDiff,
    ) -> List[Finding]:
        state: CollaborationState = {
            "task_id": task_id, "diff": diff, "parsed": parsed,
        }
        if self.graph:
            result = self.graph.invoke(state)
        else:
            result = state
            for node in (
                self._plan_node, self._specialist_node, self._critic_node,
                self._test_node, self._synthesize_node, self._fix_node,
                self._verify_node,
            ):
                result.update(node(result))
        return result["verified"]

    def _emit(
        self, state: CollaborationState, sender: str, recipient: str,
        kind: str, content: Dict[str, Any], correlation_id: str = "",
    ) -> None:
        message = AgentMessage(sender, recipient, kind, content, correlation_id)
        if self.store is not None and state.get("task_id"):
            self.store.record_agent_message(state["task_id"], message.to_dict())

    def _plan_node(self, state: CollaborationState) -> Dict[str, Any]:
        plan = self.planner.plan(state["parsed"], self.agents)
        self._emit(state, self.planner.name, "specialists", "review_plan", plan.to_dict())
        return {"plan": plan}

    def _specialist_node(self, state: CollaborationState) -> Dict[str, Any]:
        findings: List[Finding] = []
        failures = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, max(1, len(self.agents)))
        ) as pool:
            futures = {
                pool.submit(agent.review, state["diff"], state["parsed"]): agent
                for agent in self.agents
            }
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    output = future.result()
                    findings.extend(output)
                    self._emit(
                        state, agent.name, self.critic.name, "specialist_evidence",
                        {"findings": [item.to_dict() for item in output]},
                    )
                except Exception as exc:
                    failures.append("%s: %s" % (agent.name, exc))
                    self._emit(
                        state, agent.name, self.planner.name, "agent_failure",
                        {"error": str(exc)[:1000]},
                    )
        if failures and not findings and len(failures) == len(self.agents):
            raise RuntimeError("all review agents failed: " + "; ".join(failures))
        return {"specialist_findings": findings}

    def _critic_node(self, state: CollaborationState) -> Dict[str, Any]:
        critiques = {}
        for finding in state["specialist_findings"]:
            critique = self.critic.challenge(finding, state["parsed"])
            critiques[critique.finding_key] = critique
            self._emit(
                state, self.critic.name, self.test_agent.name, "critique",
                asdict(critique), critique.finding_key,
            )
        return {"critiques": critiques}

    def _test_node(self, state: CollaborationState) -> Dict[str, Any]:
        reproductions = {}
        for finding in state["specialist_findings"]:
            reproduction = self.test_agent.reproduce(finding, state["parsed"])
            reproductions[reproduction.finding_key] = reproduction
            self._emit(
                state, self.test_agent.name, self.synthesizer.name, "reproduction",
                asdict(reproduction), reproduction.finding_key,
            )
        return {"reproductions": reproductions}

    def _synthesize_node(self, state: CollaborationState) -> Dict[str, Any]:
        findings = self.synthesizer.synthesize(
            state["specialist_findings"], state["critiques"], state["reproductions"]
        )
        self._emit(
            state, self.synthesizer.name, self.fix_agent.name, "arbitration",
            {"accepted_findings": [item.to_dict() for item in findings]},
        )
        return {"synthesized": findings}

    def _fix_node(self, state: CollaborationState) -> Dict[str, Any]:
        fix_ready = {
            finding_key(item): self.fix_agent.assess(item)
            for item in state["synthesized"]
        }
        self._emit(
            state, self.fix_agent.name, self.verifier.name, "fix_assessment",
            {"decisions": fix_ready},
        )
        return {"fix_ready": fix_ready}

    def _verify_node(self, state: CollaborationState) -> Dict[str, Any]:
        verified = [
            item for item in state["synthesized"]
            if self.verifier.verify(
                item, state["reproductions"][finding_key(item)],
                state["fix_ready"][finding_key(item)],
            )
        ]
        self._emit(
            state, self.verifier.name, "review-report", "verification_decision",
            {"approved_findings": [item.to_dict() for item in verified]},
        )
        return {"verified": verified}
