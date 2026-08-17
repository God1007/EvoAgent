"""Fail-closed production provenance audit for independently labelled datasets."""

from __future__ import annotations

import hashlib
import itertools
import re
import urllib.parse
from datetime import datetime
from typing import Any

from .evaluation_dataset import canonical_json, dataset_fingerprint

_PAIR_AGREEMENT_FIELDS = {
    "left_reviewer_id",
    "right_reviewer_id",
    "left_findings",
    "right_findings",
    "matched_findings",
    "finding_f1",
    "severity_matches",
    "severity_agreement",
}


def _valid_rfc3339(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _public_source_is_fixed(case: dict[str, Any]) -> bool:
    source = case.get("source") or {}
    repository = str(case.get("repository", ""))
    pull_request = str(case.get("pull_request", ""))
    try:
        public_url = urllib.parse.urlsplit(str(source.get("public_url", "")))
    except ValueError:
        return False
    expected_path = "/%s/pull/%s" % (repository, pull_request)
    diff_sha256 = hashlib.sha256(str(case.get("diff", "")).encode("utf-8")).hexdigest()
    return bool(
        source.get("kind") == "public-github-pr"
        and public_url.scheme == "https"
        and (public_url.hostname or "").lower() == "github.com"
        and public_url.path.rstrip("/") == expected_path
        and not public_url.query
        and not public_url.fragment
        and re.fullmatch(r"[0-9a-f]{40}", str(source.get("head_sha", "")))
        and source.get("diff_sha256") == diff_sha256
        and _valid_rfc3339(source.get("retrieved_at"))
    )


def _rights_are_approved(case: dict[str, Any]) -> bool:
    rights = (case.get("source") or {}).get("rights") or {}
    spdx = str(rights.get("spdx_id", "")).upper()
    return bool(
        rights.get("review_status") == "approved"
        and rights.get("data_review_status") == "approved"
        and rights.get("usage_basis")
        in {"repository-license", "author-permission", "benchmark-license"}
        and spdx not in {"", "NONE", "NOASSERTION", "UNKNOWN"}
        and re.fullmatch(r"[A-Za-z0-9.+-]{2,64}", str(rights.get("spdx_id", "")))
        and str(rights.get("review_reference", "")).strip()
    )


def _annotation_is_independent(case: dict[str, Any]) -> bool:
    try:
        annotation = case.get("annotation") or {}
        raw_annotators = annotation.get("independent_annotators") or []
        raw_packet_hashes = annotation.get("packet_sha256s") or []
        pair_agreement = annotation.get("pair_agreement") or []
        if not all(
            (
                isinstance(raw_annotators, list),
                isinstance(raw_packet_hashes, list),
                isinstance(pair_agreement, list),
            )
        ):
            return False
        annotators = [str(item) for item in raw_annotators]
        packet_hashes = [str(item) for item in raw_packet_hashes]
        adjudicator = str(annotation.get("adjudicator_id", ""))
        reviewer_pattern = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
        expected_pairs = {tuple(sorted(pair)) for pair in itertools.combinations(annotators, 2)}
        actual_pairs = set()
        valid_agreement = True
        for item in pair_agreement:
            if not isinstance(item, dict) or set(item) != _PAIR_AGREEMENT_FIELDS:
                return False
            left = str(item.get("left_reviewer_id", ""))
            right = str(item.get("right_reviewer_id", ""))
            actual_pairs.add(tuple(sorted((left, right))))
            left_count = int(item.get("left_findings", -1))
            right_count = int(item.get("right_findings", -1))
            matched = int(item.get("matched_findings", -1))
            finding_f1 = float(item.get("finding_f1", -1))
            severity_matches = int(item.get("severity_matches", -1))
            severity_agreement = float(item.get("severity_agreement", -1))
            denominator = left_count + right_count
            expected_f1 = round(2 * matched / denominator, 4) if denominator else 1.0
            expected_severity = round(severity_matches / matched, 4) if matched else 0.0
            valid_agreement = valid_agreement and bool(
                left_count >= 0
                and right_count >= 0
                and 0 <= matched <= min(left_count, right_count)
                and abs(finding_f1 - expected_f1) <= 0.0001
                and 0 <= severity_matches <= matched
                and 0 <= severity_agreement <= 1
                and abs(severity_agreement - expected_severity) <= 0.0001
            )
        return bool(
            case.get("schema_version") == 2
            and annotation.get("schema_version") == 1
            and annotation.get("blind_to_system_output") is True
            and len(annotators) >= 2
            and len(annotators) == len(set(annotators))
            and all(reviewer_pattern.fullmatch(item) for item in annotators)
            and reviewer_pattern.fullmatch(adjudicator)
            and adjudicator not in annotators
            and len(packet_hashes) == len(annotators) + 1
            and len(packet_hashes) == len(set(packet_hashes))
            and all(re.fullmatch(r"[0-9a-f]{64}", item) for item in packet_hashes)
            and len(pair_agreement) == len(expected_pairs)
            and actual_pairs == expected_pairs
            and valid_agreement
            and str(annotation.get("protocol_id", "")).strip()
        )
    except (TypeError, ValueError):
        return False


def audit_dataset_provenance(
    cases: list[dict[str, Any]],
    annotation_evidence: dict[str, Any] | None = None,
    minimum_cases: int = 50,
) -> dict[str, Any]:
    """Audit structural evidence; human rights/data approvals remain assertions."""
    validation_repositories = {
        str(case.get("repository", "")) for case in cases if case.get("split") == "validation"
    }
    holdout_repositories = {
        str(case.get("repository", "")) for case in cases if case.get("split") == "holdout"
    }
    source_kinds = {str((case.get("source") or {}).get("kind", "unknown")) for case in cases}
    protocols = {str((case.get("annotation") or {}).get("protocol_id", "")) for case in cases}
    diff_hashes = [
        hashlib.sha256(str(case.get("diff", "")).encode("utf-8")).hexdigest() for case in cases
    ]
    packet_hashes = sorted(
        str(packet_hash)
        for case in cases
        for packet_hash in ((case.get("annotation") or {}).get("packet_sha256s") or [])
    )
    pair_agreements = [
        agreement
        for case in cases
        for agreement in ((case.get("annotation") or {}).get("pair_agreement") or [])
        if isinstance(agreement, dict)
    ]
    agreement_values_valid = True
    try:
        macro_finding_f1 = (
            round(
                sum(float(item.get("finding_f1", 0.0)) for item in pair_agreements)
                / len(pair_agreements),
                4,
            )
            if pair_agreements
            else 0.0
        )
        matched_findings = sum(int(item.get("matched_findings", 0)) for item in pair_agreements)
        severity_agreement = (
            round(
                sum(int(item.get("severity_matches", 0)) for item in pair_agreements)
                / matched_findings,
                4,
            )
            if matched_findings
            else 0.0
        )
        positive_pair_agreements = [
            item
            for item in pair_agreements
            if int(item.get("left_findings", 0)) or int(item.get("right_findings", 0))
        ]
        positive_finding_f1 = (
            round(
                sum(float(item.get("finding_f1", 0.0)) for item in positive_pair_agreements)
                / len(positive_pair_agreements),
                4,
            )
            if positive_pair_agreements
            else 0.0
        )
    except (TypeError, ValueError):
        agreement_values_valid = False
        macro_finding_f1 = -1.0
        severity_agreement = -1.0
        positive_pair_agreements = []
        positive_finding_f1 = -1.0
    split_has_risk_and_clean = all(
        any(case.get("split") == split and case.get("expected_findings") for case in cases)
        and any(case.get("split") == split and not case.get("expected_findings") for case in cases)
        for split in ("validation", "holdout")
    )
    expected_bundle_sha256 = hashlib.sha256(
        canonical_json(packet_hashes).encode("utf-8")
    ).hexdigest()
    evidence = annotation_evidence or {}
    evidence_agreement = evidence.get("agreement") or {}
    evidence_matches = bool(
        evidence.get("schema_version") == 1
        and evidence.get("status") == "compiled"
        and evidence.get("case_count") == len(cases)
        and evidence.get("repository_count")
        == len({str(case.get("repository", "")) for case in cases})
        and evidence.get("dataset_sha256") == dataset_fingerprint(cases)
        and evidence.get("annotation_bundle_sha256") == expected_bundle_sha256
        and evidence.get("protocol_ids") == sorted(protocols)
        and agreement_values_valid
        and evidence_agreement.get("annotator_pair_count") == len(pair_agreements)
        and evidence_agreement.get("macro_finding_f1") == macro_finding_f1
        and evidence_agreement.get("positive_pair_count") == len(positive_pair_agreements)
        and evidence_agreement.get("positive_pair_macro_finding_f1") == positive_finding_f1
        and evidence_agreement.get("both_clean_pair_count")
        == len(pair_agreements) - len(positive_pair_agreements)
        and evidence_agreement.get("matched_severity_agreement") == severity_agreement
    )
    checks = {
        "minimum_sample": len(cases) >= minimum_cases,
        "repository_disjoint_splits": not (validation_repositories & holdout_repositories),
        "holdout_coverage": bool(
            validation_repositories and len(holdout_repositories) >= 2 and split_has_risk_and_clean
        ),
        "unique_case_content": len(diff_hashes) == len(set(diff_hashes)),
        "immutable_public_sources": bool(cases)
        and all(_public_source_is_fixed(case) for case in cases),
        "approved_usage_and_data_review": bool(cases)
        and all(_rights_are_approved(case) for case in cases),
        "independent_blind_annotations": bool(cases)
        and all(_annotation_is_independent(case) for case in cases),
        "single_annotation_protocol": len(protocols) == 1 and "" not in protocols,
        "annotation_evidence_binding": evidence_matches,
    }
    return {
        "production_ready": all(checks.values()),
        "checks": {name: {"passed": passed} for name, passed in checks.items()},
        "source_kinds": sorted(source_kinds),
        "case_count": len(cases),
        "validation_repositories": len(validation_repositories),
        "holdout_repositories": len(holdout_repositories),
        "minimum_cases": minimum_cases,
    }
