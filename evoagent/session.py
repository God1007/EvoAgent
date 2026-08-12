"""PR review sessions: continuity across multiple pushes to one pull request.

A GitHub pull request lives through several ``synchronize`` events. Each event
today spawns an isolated review task, so a finding reported on push A and still
present on push B looks brand new, and a finding fixed on push B is never closed.

This module models a *session* (one per tenant+repository+pull_request) made of
ordered *turns* (one per reviewed head SHA). The pure functions here classify a
turn's findings against the previous turn so the delivery layer can:

* carry a still-open finding forward instead of re-announcing it,
* auto-close a finding that disappeared,
* recognise a finding that merely moved to another file/line as the same issue.

Identity is the repository-scoped, line-independent ``Finding.scoped_fingerprint``
so unrelated edits above a finding, or a pure reindentation, do not break
continuity. "Moved" is detected with a path-independent key so a renamed file
does not masquerade as "one resolved + one new" issue.
"""

from dataclasses import dataclass
from enum import Enum

from .models import _WHITESPACE, Finding


class FindingStatus(str, Enum):
    NEW = "new"
    STILL_OPEN = "still_open"
    RESOLVED = "resolved"
    MOVED = "moved"


@dataclass
class ClassifiedFinding:
    fingerprint: str
    status: FindingStatus
    finding: Finding | None
    previous_path: str | None = None

    def to_dict(self) -> dict:
        value: dict = {"fingerprint": self.fingerprint, "status": self.status.value}
        if self.finding is not None:
            value["finding"] = self.finding.to_dict()
        if self.previous_path is not None:
            value["previous_path"] = self.previous_path
        return value


def _moved_key(rule_id: str, title: str, evidence: str) -> tuple[str, str, str]:
    """Path-independent identity used to reconnect a finding across a file move."""
    return (
        rule_id,
        _WHITESPACE.sub(" ", title).strip(),
        _WHITESPACE.sub(" ", evidence).strip(),
    )


def classify_findings(
    repository: str,
    previous: list[dict],
    current: list[Finding],
) -> list[ClassifiedFinding]:
    """Diff the current findings against the previous turn's snapshot.

    ``previous`` is the stored snapshot of the last turn's findings: each item
    must carry at least ``fingerprint``, ``rule_id``, ``title``, ``evidence`` and
    ``path`` (as produced by :func:`snapshot_findings`). ``current`` are freshly
    produced :class:`Finding` objects.

    Returns one :class:`ClassifiedFinding` per distinct issue: every current
    finding (NEW / STILL_OPEN / MOVED) plus every resolved previous finding.
    """
    previous_by_fp = {item["fingerprint"]: item for item in previous}
    current_by_fp: dict[str, Finding] = {
        item.scoped_fingerprint(repository): item for item in current
    }

    results: list[ClassifiedFinding] = []
    unmatched_previous = dict(previous_by_fp)

    # First pass: exact fingerprint match => still open (line-independent).
    carried: dict[str, Finding] = {}
    for fingerprint, finding in current_by_fp.items():
        if fingerprint in previous_by_fp:
            unmatched_previous.pop(fingerprint, None)
            results.append(ClassifiedFinding(fingerprint, FindingStatus.STILL_OPEN, finding))
        else:
            carried[fingerprint] = finding

    # Second pass: reconnect moves via a path-independent key. Several previous
    # findings can share the key (boilerplate like ``except: pass``), so buckets
    # hold a list and each current finding consumes at most one entry — this
    # avoids collapsing N genuine moves into "1 moved + (N-1) new/resolved".
    previous_by_move: dict[tuple, list[dict]] = {}
    for item in unmatched_previous.values():
        key = _moved_key(item.get("rule_id", ""), item.get("title", ""), item.get("evidence", ""))
        previous_by_move.setdefault(key, []).append(item)
    for fingerprint, finding in carried.items():
        key = _moved_key(finding.rule_id, finding.title, finding.evidence)
        bucket = previous_by_move.get(key) or []
        prior = bucket.pop(0) if bucket else None
        if prior is not None:
            unmatched_previous.pop(prior["fingerprint"], None)
            results.append(
                ClassifiedFinding(
                    fingerprint,
                    FindingStatus.MOVED,
                    finding,
                    previous_path=prior.get("path"),
                )
            )
        else:
            results.append(ClassifiedFinding(fingerprint, FindingStatus.NEW, finding))

    # Whatever remains in the previous snapshot has disappeared => resolved.
    for item in unmatched_previous.values():
        results.append(ClassifiedFinding(item["fingerprint"], FindingStatus.RESOLVED, None))
    return results


def snapshot_findings(repository: str, findings: list[Finding]) -> list[dict]:
    """Produce the persistable snapshot the next turn will classify against."""
    snapshot = []
    for finding in findings:
        snapshot.append(
            {
                "fingerprint": finding.scoped_fingerprint(repository),
                "rule_id": finding.rule_id,
                "title": finding.title,
                "evidence": finding.evidence,
                "path": finding.path,
                "line": finding.line,
                "severity": finding.severity.value,
            }
        )
    return snapshot


def open_snapshot(repository: str, classified: list[ClassifiedFinding]) -> list[dict]:
    """Persistable snapshot of the still-open findings, tagged with status.

    Resolved findings are intentionally dropped: the snapshot represents the set
    of open issues at the end of a turn, which the *next* turn diffs against.
    """
    snapshots = []
    for item in classified:
        if item.finding is None:  # resolved
            continue
        record = snapshot_findings(repository, [item.finding])[0]
        record["status"] = item.status.value
        if item.previous_path is not None:
            record["previous_path"] = item.previous_path
        snapshots.append(record)
    return snapshots


def continuity_summary(classified: list[ClassifiedFinding]) -> dict:
    """Counts used by the delivery layer, the timeline API and metrics."""
    summary = {status.value: 0 for status in FindingStatus}
    for item in classified:
        summary[item.status.value] += 1
    summary["carried"] = (
        summary[FindingStatus.STILL_OPEN.value] + summary[FindingStatus.MOVED.value]
    )
    summary["open"] = (
        summary[FindingStatus.NEW.value]
        + summary[FindingStatus.STILL_OPEN.value]
        + summary[FindingStatus.MOVED.value]
    )
    return summary
