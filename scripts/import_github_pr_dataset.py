"""Import immutable public GitHub PR inputs for the independent-label workflow.

Public PR content is not ground truth. The safe default emits answer-free cases
which must later pass through ``evoagent-eval-labels``. A legacy labelled mode is
retained for controlled fixtures but can never satisfy the production gate.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.diff_parser import parse_unified_diff  # noqa: E402
from evoagent.evaluation_dataset import validate_case  # noqa: E402
from evoagent.github import GitHubClient  # noqa: E402
from evoagent.json_boundary import strict_json_loads  # noqa: E402


def _public_pr_head(payload, repository, pull_request):
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned invalid pull request metadata")
    base_repository = (payload.get("base") or {}).get("repo") or {}
    try:
        number = int(payload.get("number", -1))
    except (TypeError, ValueError):
        number = -1
    if (
        number != pull_request
        or str(base_repository.get("full_name", "")).lower() != repository.lower()
        or base_repository.get("private") is not False
    ):
        raise RuntimeError("pull request is not from the requested public repository")
    head_sha = str((payload.get("head") or {}).get("sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise RuntimeError("pull request head was invalid during import")
    return head_sha


def fetch_pull_request(repository, pull_request, token=""):
    url = "https://api.github.com/repos/%s/pulls/%d" % (repository, pull_request)
    client = GitHubClient(token, timeout=60, max_response_bytes=5 * 1024 * 1024)
    before = client.get_pull_request(repository, pull_request)
    before_sha = _public_pr_head(before, repository, pull_request)
    diff = client.fetch_diff(url, max_bytes=5 * 1024 * 1024)
    after = client.get_pull_request(repository, pull_request)
    after_sha = _public_pr_head(after, repository, pull_request)
    if before_sha != after_sha:
        raise RuntimeError("pull request head changed during import")
    return diff, before_sha


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="Rights-reviewed, answer-free JSONL manifest")
    parser.add_argument("output", help="Evaluation JSONL output")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--legacy-labelled",
        action="store_true",
        help="Retain embedded expected_findings for controlled, non-production fixtures",
    )
    args = parser.parse_args()
    if not 0 < args.limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")
    if os.path.abspath(args.manifest) == os.path.abspath(args.output):
        raise ValueError("manifest and output must be different files")
    token = os.environ.get("GITHUB_TOKEN", "")
    records = []
    case_ids = set()
    pull_requests = set()
    with open(args.manifest, encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                item = strict_json_loads(raw)
            except (ValueError, RecursionError) as exc:
                raise ValueError("manifest line %d is invalid JSON" % line_number) from exc
            if not isinstance(item, dict):
                raise ValueError("manifest line %d must be a JSON object" % line_number)
            if "expected_findings" in item and not args.legacy_labelled:
                raise ValueError(
                    "manifest line %d contains answer data; use independent annotation packets"
                    % line_number
                )
            rights = item.get("rights")
            if not isinstance(rights, dict):
                raise ValueError("manifest line %d has no rights-review record" % line_number)
            if not (
                rights.get("review_status") == "approved"
                and rights.get("data_review_status") == "approved"
                and rights.get("usage_basis")
                in {"repository-license", "author-permission", "benchmark-license"}
                and re.fullmatch(r"[A-Za-z0-9.+-]{2,64}", str(rights.get("spdx_id", "")))
                and str(rights.get("spdx_id", "")).upper() not in {"NONE", "NOASSERTION", "UNKNOWN"}
                and str(rights.get("review_reference", "")).strip()
            ):
                raise ValueError(
                    "manifest line %d has an unapproved rights-review record" % line_number
                )
            repository = str(item.get("repository", ""))
            try:
                pull_request = int(item.get("pull_request", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "manifest line %d has an invalid pull request" % line_number
                ) from exc
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                raise ValueError("manifest line %d has an invalid repository" % line_number)
            if pull_request <= 0:
                raise ValueError("manifest line %d has an invalid pull request" % line_number)
            split = str(item.get("split", ""))
            if split not in {"validation", "holdout"}:
                raise ValueError("manifest line %d has an invalid split" % line_number)
            case_id = str(item.get("id", "%s#%s" % (repository, pull_request))).strip()
            if not case_id or len(case_id) > 256 or any(ord(value) < 32 for value in case_id):
                raise ValueError("manifest line %d has an invalid case id" % line_number)
            source_identity = (repository.lower(), pull_request)
            if case_id in case_ids or source_identity in pull_requests:
                raise ValueError("manifest line %d duplicates a case or pull request" % line_number)
            case_ids.add(case_id)
            pull_requests.add(source_identity)
            diff, head_sha = fetch_pull_request(repository, pull_request, token)
            parsed = parse_unified_diff(diff)
            record = {
                "schema_version": 1,
                "id": case_id,
                "repository": repository,
                "pull_request": pull_request,
                "split": split,
                "source": {
                    "kind": "public-github-pr",
                    "public_url": "https://github.com/%s/pull/%d" % (repository, pull_request),
                    "head_sha": head_sha,
                    "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                    "retrieved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "rights": rights,
                },
                "diff": diff,
            }
            if args.legacy_labelled:
                if "expected_findings" not in item:
                    raise ValueError(
                        "manifest line %d has no expected_findings for legacy mode" % line_number
                    )
                record.update(
                    {
                        "after_files": item.get("after_files", {}),
                        "expected_findings": item["expected_findings"],
                        "repair_validation": item.get("repair_validation", {}),
                    }
                )
                validate_case(record)
            if not parsed.added_lines:
                raise ValueError("PR %s has no added lines" % record["id"])
            records.append(record)
            if len(records) >= args.limit:
                break
    if len(records) < args.limit:
        raise ValueError("manifest produced %d records; %d required" % (len(records), args.limit))
    output_directory = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(output_directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".public-pr-import.", dir=output_directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    kind = "legacy-labelled" if args.legacy_labelled else "answer-free"
    print("wrote %d %s public PRs to %s" % (len(records), kind, args.output))


if __name__ == "__main__":
    main()
