import hashlib
import json
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from enum import Enum
from typing import Any

_WHITESPACE = re.compile(r"\s+")


class TaskState(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REVIEWING = "REVIEWING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class ChangedLine:
    path: str
    line: int
    content: str


@dataclass
class Finding:
    rule_id: str
    severity: Severity
    title: str
    explanation: str
    path: str
    line: int
    evidence: str
    fix: str
    test: str
    confidence: float = 0.8

    def fingerprint(self) -> str:
        """Stable, line-independent identity for a finding.

        Absolute line numbers move whenever unrelated code changes above a
        finding, so they cannot identify "the same issue" across PR revisions.
        The fingerprint is derived from the file, rule, title and the
        whitespace-normalised offending snippet, so it is stable against
        reindentation and edits elsewhere in the file. Fields are JSON-encoded
        before hashing so no field value can be confused with a delimiter.

        It is consumed today by shadow-comparison finding-set equality and is
        embedded in each serialised finding (``to_dict``) so downstream storage
        can de-duplicate across revisions. Note: two findings that share file,
        rule, title and normalised evidence collapse to one fingerprint by
        design — they are treated as the same issue.

        Caveat: ``title`` participates in the identity so that findings with
        empty/identical evidence but different messages do not collide. For the
        LLM reviewer ``title`` is free text, so a reworded title on a later
        revision yields a new fingerprint and defeats cross-revision de-dup.
        Rule-based findings have deterministic titles and are unaffected.
        """
        normalized_evidence = _WHITESPACE.sub(" ", self.evidence).strip()
        normalized_title = _WHITESPACE.sub(" ", self.title).strip()
        raw = json.dumps(
            [self.path, self.rule_id, normalized_title, normalized_evidence],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["severity"] = self.severity.value
        value["fingerprint"] = self.fingerprint()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Finding":
        """Build a Finding, tolerating derived/unknown keys such as ``fingerprint``.

        Severity is case-normalised and falls back to MEDIUM when absent or
        unrecognised (matching the reviewer's defensive behaviour); any other
        missing required field raises a clear ``ValueError``.
        """
        allowed = {item.name for item in fields(cls)}
        data = {key: value[key] for key in value if key in allowed}
        severity = data.get("severity")
        if severity is None:
            data["severity"] = Severity.MEDIUM
        elif not isinstance(severity, Severity):
            try:
                data["severity"] = Severity(str(severity).lower())
            except ValueError:
                data["severity"] = Severity.MEDIUM
        required = {
            item.name
            for item in fields(cls)
            if item.default is MISSING and item.default_factory is MISSING
        }
        missing = required - set(data)
        if missing:
            raise ValueError("finding is missing required fields: %s" % ", ".join(sorted(missing)))
        return cls(**data)


@dataclass
class ReviewReport:
    repository: str
    pull_request: int | None
    summary: str
    risk: str
    findings: list[Finding] = field(default_factory=list)
    files_reviewed: list[str] = field(default_factory=list)
    reviewer: str = "local-rules"

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "pull_request": self.pull_request,
            "summary": self.summary,
            "risk": self.risk,
            "findings": [item.to_dict() for item in self.findings],
            "files_reviewed": self.files_reviewed,
            "reviewer": self.reviewer,
        }


@dataclass
class TraceEvent:
    step: int
    state: TaskState
    message: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value
