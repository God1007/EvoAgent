"""Import labelled public GitHub PR diffs into the Evaluation Harness JSONL format.

The manifest must contain repository, pull_request, split, expected_findings and
repair_validation. Public PR content alone is not ground truth, so unlabelled
records are intentionally rejected.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.diff_parser import parse_unified_diff  # noqa: E402
from evoagent.evaluation_harness import validate_case  # noqa: E402


def fetch_diff(repository, pull_request, token=""):
    url = "https://api.github.com/repos/%s/pulls/%d" % (repository, pull_request)
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "User-Agent": "evoagent-evaluation-importer",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace"), url
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "GitHub returned HTTP %d for %s#%d"
            % (exc.code, repository, pull_request)
        ) from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="Labelled JSONL manifest")
    parser.add_argument("output", help="Evaluation JSONL output")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    records = []
    with open(args.manifest, "r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            item = json.loads(raw)
            if "expected_findings" not in item:
                raise ValueError(
                    "manifest line %d has no human-reviewed expected_findings"
                    % line_number
                )
            diff, _api_url = fetch_diff(
                str(item["repository"]), int(item["pull_request"]), token
            )
            parsed = parse_unified_diff(diff)
            record = {
                "schema_version": 1,
                "id": item.get(
                    "id", "%s#%s" % (item["repository"], item["pull_request"])
                ),
                "repository": item["repository"],
                "pull_request": int(item["pull_request"]),
                "split": item["split"],
                "source": {
                    "kind": "public-github-pr",
                    "public_url": "https://github.com/%s/pull/%d"
                    % (item["repository"], int(item["pull_request"])),
                },
                "diff": diff,
                "after_files": item.get("after_files", {}),
                "expected_findings": item["expected_findings"],
                "repair_validation": item.get("repair_validation", {}),
            }
            validate_case(record)
            if not parsed.added_lines:
                raise ValueError("PR %s has no added lines" % record["id"])
            records.append(record)
            if len(records) >= args.limit:
                break
    if len(records) < args.limit:
        raise ValueError(
            "manifest produced %d records; %d required" % (len(records), args.limit)
        )
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    print("wrote %d labelled public PRs to %s" % (len(records), args.output))


if __name__ == "__main__":
    main()
