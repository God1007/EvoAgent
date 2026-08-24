import hashlib
import json
import math
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from enum import Enum
from typing import Any

_WHITESPACE = re.compile(r"\s+")
MAX_RULE_ID_CHARS = 80
FINDING_TEXT_LIMITS = {
    "title": 200,
    "explanation": 2000,
    "path": 4096,
    "evidence": 240,
    "fix": 2000,
    "test": 2000,
}


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

    def scoped_fingerprint(self, repository: str, symbol: str = "") -> str:
        """Repository-scoped identity for cross-PR / cross-repo de-duplication.

        ``fingerprint`` identifies a finding *within a single review*, but two
        different repositories can legitimately produce the same file, rule and
        evidence. The session and repository-memory layers must therefore bind
        the repository (and, once the code graph lands, a normalised symbol) so a
        finding in repo A is never confused with an identical-looking finding in
        repo B. ``symbol`` defaults to the file path as a stable proxy until
        real symbol extraction is available.

        Composition follows the roadmap: repository + rule_id + normalised
        symbol + title + semantic-evidence hash, JSON-encoded so no field value
        can be confused with a delimiter.
        """
        normalized_evidence = _WHITESPACE.sub(" ", self.evidence).strip()
        normalized_title = _WHITESPACE.sub(" ", self.title).strip()
        normalized_symbol = symbol or self.path
        raw = json.dumps(
            [repository, self.rule_id, normalized_symbol, normalized_title, normalized_evidence],
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
        text_fields = ("rule_id", "title", "explanation", "path", "evidence", "fix", "test")
        invalid_text = [name for name in text_fields if not isinstance(data[name], str)]
        if invalid_text:
            raise ValueError("finding fields must be strings: %s" % ", ".join(sorted(invalid_text)))
        try:
            if any("\x00" in data[name] for name in text_fields):
                raise UnicodeError
            for name in text_fields:
                data[name].encode("utf-8")
        except UnicodeError:
            raise ValueError("finding fields must be valid UTF-8 without NUL bytes") from None
        rule_id = data["rule_id"]
        if (
            not rule_id
            or rule_id != rule_id.strip()
            or len(rule_id) > MAX_RULE_ID_CHARS
            or not all(character.isprintable() and not character.isspace() for character in rule_id)
        ):
            raise ValueError(
                "finding rule_id must be a printable whitespace-free token of at most %d characters"
                % MAX_RULE_ID_CHARS
            )
        oversized = [name for name, limit in FINDING_TEXT_LIMITS.items() if len(data[name]) > limit]
        if oversized:
            raise ValueError("finding fields exceed size limits: %s" % ", ".join(sorted(oversized)))
        if not isinstance(data["line"], int) or isinstance(data["line"], bool) or data["line"] < 1:
            raise ValueError("finding line must be a positive integer")
        confidence = data.get("confidence", 0.8)
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("finding confidence must be a finite number between 0 and 1")
        data["confidence"] = float(confidence)
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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReviewReport":
        if not isinstance(value, dict):
            raise ValueError("review report must be an object")
        missing = {"repository", "summary", "risk"}.difference(value)
        if missing:
            raise ValueError(
                "review report is missing required fields: %s" % ", ".join(sorted(missing))
            )
        reviewer = value.get("reviewer", "unknown")
        if any(
            not isinstance(item, str)
            for item in (value["repository"], value["summary"], value["risk"], reviewer)
        ):
            raise ValueError("review report text fields must be strings")
        if not value["repository"] or value["risk"] not in {"low", "medium", "high"}:
            raise ValueError("review report repository and risk are invalid")
        pull_request = value.get("pull_request")
        if pull_request is not None and (
            isinstance(pull_request, bool) or not isinstance(pull_request, int) or pull_request <= 0
        ):
            raise ValueError("review report pull_request must be a positive integer or null")
        raw_findings = value.get("findings", [])
        if not isinstance(raw_findings, list) or any(
            not isinstance(item, dict) for item in raw_findings
        ):
            raise ValueError("review report findings must be a list of objects")
        files_reviewed = value.get("files_reviewed", [])
        if not isinstance(files_reviewed, list) or any(
            not isinstance(item, str) for item in files_reviewed
        ):
            raise ValueError("review report files_reviewed must be a list of strings")
        return cls(
            repository=value["repository"],
            pull_request=pull_request,
            summary=value["summary"],
            risk=value["risk"],
            findings=[Finding.from_dict(item) for item in raw_findings],
            files_reviewed=files_reviewed,
            reviewer=reviewer,
        )

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
