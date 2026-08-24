"""Compile independently blind-reviewed annotations into evaluation ground truth."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from .evaluation_dataset import dataset_fingerprint, validate_case
from .json_boundary import strict_json_loads

ANNOTATION_SCHEMA_VERSION = 1
COMPILED_CASE_SCHEMA_VERSION = 2
_REVIEWER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_PROTOCOL_ID = re.compile(r"^[a-z0-9][a-z0-9._/-]{2,127}$")
_CWE = re.compile(r"^CWE-[1-9][0-9]*$")
_PACKET_FIELDS = {
    "schema_version",
    "case_id",
    "role",
    "reviewer_id",
    "protocol_id",
    "blind_to_system_output",
    "findings",
}
_FINDING_FIELDS = {"path", "start_line", "end_line", "cwe", "severity"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def load_records(path: str | Path) -> list[dict[str, Any]]:
    records = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                record = strict_json_loads(raw)
            except (ValueError, RecursionError) as exc:
                raise ValueError("invalid JSON on line %d of %s" % (line_number, path)) from exc
            if not isinstance(record, dict):
                raise ValueError("line %d of %s must be a JSON object" % (line_number, path))
            records.append(record)
    return records


def _normalize_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    return value[2:] if value.startswith(("a/", "b/")) else value


def _validate_base_cases(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not cases:
        raise ValueError("the unlabelled case set is empty")
    by_id: dict[str, dict[str, Any]] = {}
    repository_splits: dict[str, set[str]] = {}
    for case in cases:
        case_id = str(case.get("id", ""))
        if not case_id or case_id in by_id:
            raise ValueError("unlabelled cases must have unique non-empty ids")
        if "expected_findings" in case or "annotation" in case:
            raise ValueError("unlabelled case %s already contains answer data" % case_id)
        if not isinstance(case.get("source"), dict):
            raise ValueError("unlabelled case %s is missing source provenance" % case_id)
        candidate = {**case, "expected_findings": []}
        validate_case(candidate)
        by_id[case_id] = case
        repository = str(case["repository"])
        repository_splits.setdefault(repository, set()).add(str(case["split"]))
    leaked = sorted(
        repository for repository, splits in repository_splits.items() if len(splits) > 1
    )
    if leaked:
        raise ValueError(
            "repositories appear in both validation and holdout: %s" % ", ".join(leaked)
        )
    return by_id


def _validate_findings(
    case: dict[str, Any], findings: Any, packet_description: str
) -> list[dict[str, Any]]:
    if not isinstance(findings, list):
        raise ValueError("%s findings must be an array" % packet_description)
    normalized = []
    seen = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
            raise ValueError("%s has a finding with an invalid field set" % packet_description)
        value = {
            "path": _normalize_path(str(finding["path"])),
            "start_line": int(finding["start_line"]),
            "end_line": int(finding["end_line"]),
            "cwe": str(finding["cwe"]).upper(),
            "severity": str(finding["severity"]).lower(),
        }
        if not _CWE.fullmatch(str(value["cwe"])):
            raise ValueError("%s has an invalid CWE" % packet_description)
        fingerprint = _canonical_json(value)
        if fingerprint in seen:
            raise ValueError("%s contains a duplicate finding" % packet_description)
        seen.add(fingerprint)
        normalized.append(value)
    validate_case({**case, "expected_findings": normalized})
    return normalized


def _validate_packets(
    cases: dict[str, dict[str, Any]], packets: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in cases}
    identities = set()
    for packet in packets:
        if set(packet) != _PACKET_FIELDS:
            raise ValueError("annotation packet has an invalid field set")
        if packet["schema_version"] != ANNOTATION_SCHEMA_VERSION:
            raise ValueError("annotation packet has an unsupported schema version")
        case_id = str(packet["case_id"])
        if case_id not in cases:
            raise ValueError("annotation packet references unknown case %s" % case_id)
        role = str(packet["role"])
        if role not in {"annotation", "adjudication"}:
            raise ValueError("annotation packet %s has an invalid role" % case_id)
        reviewer_id = str(packet["reviewer_id"])
        protocol_id = str(packet["protocol_id"])
        if not _REVIEWER_ID.fullmatch(reviewer_id):
            raise ValueError("annotation reviewer ids must be opaque lowercase identifiers")
        if not _PROTOCOL_ID.fullmatch(protocol_id):
            raise ValueError("annotation protocol id is invalid")
        if packet["blind_to_system_output"] is not True:
            raise ValueError("every annotation and adjudication must be blind to system output")
        identity = (case_id, role, reviewer_id)
        if identity in identities:
            raise ValueError("duplicate annotation packet for %s" % case_id)
        identities.add(identity)
        normalized = {
            **packet,
            "case_id": case_id,
            "role": role,
            "reviewer_id": reviewer_id,
            "protocol_id": protocol_id,
            "findings": _validate_findings(
                cases[case_id], packet["findings"], "%s packet for %s" % (role, case_id)
            ),
        }
        normalized["packet_sha256"] = _sha256(normalized)
        grouped[case_id].append(normalized)
    return grouped


def _ranges_distance(left: dict[str, Any], right: dict[str, Any]) -> int:
    left_start, left_end = int(left["start_line"]), int(left["end_line"])
    right_start, right_end = int(right["start_line"]), int(right["end_line"])
    if left_start <= right_end and right_start <= left_end:
        return 0
    return min(abs(left_start - right_end), abs(right_start - left_end))


def _label_matches(
    left: list[dict[str, Any]], right: list[dict[str, Any]], line_tolerance: int = 2
) -> list[tuple[int, int]]:
    edges: dict[int, list[int]] = {}
    for left_index, expected in enumerate(left):
        edges[left_index] = [
            right_index
            for right_index, candidate in enumerate(right)
            if expected["path"] == candidate["path"]
            and expected["cwe"] == candidate["cwe"]
            and _ranges_distance(expected, candidate) <= line_tolerance
        ]
    owner: dict[int, int] = {}

    def assign(left_index: int, visited: set[int]) -> bool:
        for right_index in edges[left_index]:
            if right_index in visited:
                continue
            visited.add(right_index)
            previous = owner.get(right_index)
            if previous is None or assign(previous, visited):
                owner[right_index] = left_index
                return True
        return False

    for left_index in sorted(edges, key=lambda index: (len(edges[index]), index)):
        assign(left_index, set())
    return sorted((left_index, right_index) for right_index, left_index in owner.items())


def _pair_agreement(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> dict[str, Any]:
    matches = _label_matches(left, right)
    denominator = len(left) + len(right)
    finding_f1 = round((2 * len(matches)) / denominator, 4) if denominator else 1.0
    severity_hits = sum(left[a]["severity"] == right[b]["severity"] for a, b in matches)
    severity_agreement = round(severity_hits / len(matches), 4) if matches else 0.0
    return {
        "left_findings": len(left),
        "right_findings": len(right),
        "matched_findings": len(matches),
        "finding_f1": finding_f1,
        "severity_matches": severity_hits,
        "severity_agreement": severity_agreement,
    }


def compile_independent_annotations(
    cases: list[dict[str, Any]], packets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cases_by_id = _validate_base_cases(cases)
    packets_by_case = _validate_packets(cases_by_id, packets)
    compiled = []
    all_pair_agreements = []
    all_packet_hashes = []
    protocol_ids = set()
    for case_id in sorted(cases_by_id):
        case_packets = packets_by_case[case_id]
        annotations = sorted(
            (packet for packet in case_packets if packet["role"] == "annotation"),
            key=lambda packet: packet["reviewer_id"],
        )
        adjudications = [packet for packet in case_packets if packet["role"] == "adjudication"]
        if len(annotations) < 2:
            raise ValueError("case %s requires at least two independent annotations" % case_id)
        if len(adjudications) != 1:
            raise ValueError("case %s requires exactly one adjudication" % case_id)
        adjudication = adjudications[0]
        annotator_ids = [str(packet["reviewer_id"]) for packet in annotations]
        if adjudication["reviewer_id"] in annotator_ids:
            raise ValueError("case %s adjudicator must be independent of annotators" % case_id)
        case_protocols = {str(packet["protocol_id"]) for packet in case_packets}
        if len(case_protocols) != 1:
            raise ValueError("case %s mixes annotation protocols" % case_id)
        protocol_id = next(iter(case_protocols))
        protocol_ids.add(protocol_id)
        pair_agreements = [
            {
                "left_reviewer_id": left["reviewer_id"],
                "right_reviewer_id": right["reviewer_id"],
                **_pair_agreement(left["findings"], right["findings"]),
            }
            for left, right in itertools.combinations(annotations, 2)
        ]
        all_pair_agreements.extend(pair_agreements)
        packet_hashes = sorted(str(packet["packet_sha256"]) for packet in case_packets)
        all_packet_hashes.extend(packet_hashes)
        compiled.append(
            {
                **cases_by_id[case_id],
                "schema_version": COMPILED_CASE_SCHEMA_VERSION,
                "expected_findings": adjudication["findings"],
                "annotation": {
                    "schema_version": ANNOTATION_SCHEMA_VERSION,
                    "protocol_id": protocol_id,
                    "blind_to_system_output": True,
                    "independent_annotators": annotator_ids,
                    "adjudicator_id": adjudication["reviewer_id"],
                    "packet_sha256s": packet_hashes,
                    "pair_agreement": pair_agreements,
                },
            }
        )
    extra_case_ids = sorted(case_id for case_id, values in packets_by_case.items() if not values)
    if extra_case_ids:
        raise ValueError("cases have no annotation packets: %s" % ", ".join(extra_case_ids))
    if len(protocol_ids) != 1:
        raise ValueError("one compiled dataset cannot mix annotation protocols")
    finding_f1 = (
        round(sum(item["finding_f1"] for item in all_pair_agreements) / len(all_pair_agreements), 4)
        if all_pair_agreements
        else 0.0
    )
    matched_findings = sum(item["matched_findings"] for item in all_pair_agreements)
    severity_agreement = (
        round(
            sum(item["severity_matches"] for item in all_pair_agreements) / matched_findings,
            4,
        )
        if matched_findings
        else 0.0
    )
    positive_pair_agreements = [
        item for item in all_pair_agreements if item["left_findings"] or item["right_findings"]
    ]
    positive_finding_f1 = (
        round(
            sum(item["finding_f1"] for item in positive_pair_agreements)
            / len(positive_pair_agreements),
            4,
        )
        if positive_pair_agreements
        else 0.0
    )
    evidence = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "status": "compiled",
        "case_count": len(compiled),
        "repository_count": len({str(case["repository"]) for case in compiled}),
        "protocol_ids": sorted(protocol_ids),
        "unlabelled_cases_sha256": _sha256(sorted(cases, key=lambda case: str(case["id"]))),
        "annotation_bundle_sha256": _sha256(sorted(all_packet_hashes)),
        "dataset_sha256": dataset_fingerprint(compiled),
        "agreement": {
            "annotator_pair_count": len(all_pair_agreements),
            "macro_finding_f1": finding_f1,
            "positive_pair_count": len(positive_pair_agreements),
            "positive_pair_macro_finding_f1": positive_finding_f1,
            "both_clean_pair_count": len(all_pair_agreements) - len(positive_pair_agreements),
            "matched_severity_agreement": severity_agreement,
        },
    }
    return compiled, evidence


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile blind independent annotations into adjudicated ground truth"
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        resolved_paths = {
            args.cases.resolve(),
            args.annotations.resolve(),
            args.output.resolve(),
            args.evidence.resolve(),
        }
        if len(resolved_paths) != 4:
            raise ValueError("case, annotation, dataset, and evidence paths must all be different")
        compiled, evidence = compile_independent_annotations(
            load_records(args.cases), load_records(args.annotations)
        )
        dataset_content = "".join(
            _canonical_json(case) + "\n" for case in sorted(compiled, key=lambda case: case["id"])
        )
        _atomic_write(args.evidence, _canonical_json(evidence) + "\n")
        _atomic_write(args.output, dataset_content)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(evidence, sort_keys=True))
    return 0


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
