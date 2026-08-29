import ast
import hashlib
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, TypedDict

from .errors import ClientInputError
from .fix_rules import FixRule, default_fix_rules
from .models import MAX_RULE_ID_CHARS
from .ports import CodeHostPort
from .repository import canonical_repository
from .verifier import RepairVerifier


class FixResult(TypedDict):
    content: str
    rules: list[str]


def _repair_source(pull: Any) -> tuple[str, str, str]:
    if not isinstance(pull, dict) or not isinstance(pull.get("head"), dict):
        raise RuntimeError("code host returned an invalid pull request")
    if pull.get("state") != "open":
        raise ClientInputError("pull request is no longer open; run a new review before fixing")
    if not isinstance(pull.get("draft"), bool):
        raise RuntimeError("code host returned an invalid pull request draft state")
    if pull["draft"]:
        raise ClientInputError("pull request is a draft; mark it ready before fixing")
    source_sha = pull["head"].get("sha")
    if not isinstance(source_sha, str) or not source_sha:
        raise RuntimeError("code host returned an invalid pull request head")
    head_repository = pull["head"].get("repo")
    source_repository = (
        head_repository.get("full_name") if isinstance(head_repository, dict) else None
    )
    base = pull.get("base")
    base_ref = base.get("ref") if isinstance(base, dict) else None
    if not isinstance(source_repository, str) or not isinstance(base_ref, str) or not base_ref:
        raise RuntimeError("code host returned invalid pull request source metadata")
    try:
        source_repository = canonical_repository(source_repository)
    except ClientInputError:
        raise RuntimeError("code host returned invalid pull request source metadata") from None
    return source_sha, source_repository, base_ref


def _repair_draft_receipt(
    pull: Any, repository: str, branch: str, base_ref: str
) -> tuple[int, str]:
    head = pull.get("head") if isinstance(pull, dict) else None
    base = pull.get("base") if isinstance(pull, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    number = pull.get("number") if isinstance(pull, dict) else None
    url = pull.get("html_url") if isinstance(pull, dict) else None
    if (
        not isinstance(pull, dict)
        or pull.get("state") != "open"
        or pull.get("draft") is not True
        or not isinstance(head, dict)
        or head.get("ref") != branch
        or not isinstance(head_repo, dict)
        or str(head_repo.get("full_name", "")).casefold() != repository.casefold()
        or not isinstance(base, dict)
        or base.get("ref") != base_ref
        or not isinstance(base_repo, dict)
        or str(base_repo.get("full_name", "")).casefold() != repository.casefold()
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or not isinstance(url, str)
        or not url
    ):
        raise RuntimeError("invalid draft pull request does not match the verified repair")
    return number, url


class SafeFixer:
    """Creates conservative fixes on a dedicated branch; never writes to the PR head."""

    def __init__(
        self,
        verifier: RepairVerifier | None = None,
        rules: Iterable[FixRule] | None = None,
        max_source_bytes: int = 10 * 1024 * 1024,
    ):
        if (
            isinstance(max_source_bytes, bool)
            or not isinstance(max_source_bytes, int)
            or max_source_bytes <= 0
        ):
            raise ValueError("repair source budget must be a positive integer")
        self.verifier = verifier or RepairVerifier()
        self.max_source_bytes = max_source_bytes
        selected = default_fix_rules() if rules is None else list(rules)
        self._rules: dict[str, FixRule] = {}
        for rule in selected:
            if not isinstance(rule, FixRule):
                raise TypeError("repair rule does not implement the FixRule protocol")
            rule_id = rule.rule_id
            if (
                not isinstance(rule_id, str)
                or not rule_id
                or len(rule_id) > MAX_RULE_ID_CHARS
                or any(character.isspace() or not character.isprintable() for character in rule_id)
            ):
                raise ValueError("repair rule id must be a bounded printable token")
            if rule_id in self._rules:
                raise ValueError("duplicate repair rule id: %s" % rule_id)
            self._rules[rule_id] = rule

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(self._rules)

    def propose_line(self, line: str, finding: dict[str, Any]) -> str:
        rule_id = finding.get("rule_id")
        rule = self._rules.get(rule_id) if isinstance(rule_id, str) else None
        return rule.propose_line(line) if rule is not None else line

    def apply(self, content: str, findings: list[dict[str, Any]], path: str) -> FixResult:
        if path.endswith(".py") and hasattr(ast, "unparse"):
            structured = self._apply_python_ast(content, findings, path)
            if structured["rules"]:
                return structured
        lines = content.splitlines()
        changed: list[str] = []
        needs_os = False
        for finding in sorted(
            (x for x in findings if x.get("path") == path),
            key=lambda x: x.get("line", 0),
            reverse=True,
        ):
            index = int(finding.get("line", 0)) - 1
            if index < 0 or index >= len(lines):
                continue
            replacement = self.propose_line(lines[index], finding)
            if replacement != lines[index]:
                needs_os = needs_os or "os.environ" in replacement
                lines[index] = replacement
                rule_id = finding.get("rule_id")
                if isinstance(rule_id, str):
                    changed.append(rule_id)
        if needs_os and not any(re.match(r"\s*(import os|from os import)", line) for line in lines):
            lines.insert(0, "import os")
        return {
            "content": "\n".join(lines) + ("\n" if content.endswith("\n") else ""),
            "rules": changed,
        }

    def _apply_python_ast(
        self,
        content: str,
        findings: list[dict[str, Any]],
        path: str,
    ) -> FixResult:
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError:
            return {"content": content, "rules": []}
        targets: dict[str, set[int]] = {}
        for item in findings:
            rule_id = item.get("rule_id")
            if item.get("path") == path and isinstance(rule_id, str):
                targets.setdefault(rule_id, set()).add(int(item.get("line", 0)))
        changed: list[str] = []
        required_imports: set[str] = set()
        for rule_id, rule in self._rules.items():
            target_lines = frozenset(targets.get(rule_id, set()))
            if not target_lines:
                continue
            mutation = rule.apply_python(tree, target_lines)
            if mutation.changed:
                changed.append(rule_id)
                required_imports.update(mutation.required_imports)
        if not changed:
            return {"content": content, "rules": []}
        for module in sorted(required_imports, reverse=True):
            if not self._has_import(tree, module):
                tree.body.insert(0, ast.Import(names=[ast.alias(name=module)]))
        ast.fix_missing_locations(tree)
        value = ast.unparse(tree) + "\n"
        compile(value, path, "exec")
        return {"content": value, "rules": sorted(set(changed))}

    @staticmethod
    def _has_import(tree: ast.Module, module: str) -> bool:
        return any(
            isinstance(node, ast.Import)
            and any(alias.name == module and alias.asname in {None, module} for alias in node.names)
            for node in tree.body
        )

    def create_fix_commits(
        self,
        client: CodeHostPort,
        repository: str,
        pull_request: int,
        report: dict[str, Any],
        operation_key: str = "",
        allowed_rule_ids: Iterable[str] | None = None,
        expected_source_sha: str = "",
        before_publish: Callable[[tuple[str, ...]], None] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(self.verifier.test_command, str)
            or not self.verifier.test_command.strip()
        ):
            note = "Repair was blocked because no repository test command is configured."
            return {
                "branch": None,
                "source_sha": None,
                "commits": [],
                "verification": {
                    "passed": False,
                    "status": "skipped",
                    "checks": [],
                    "note": note,
                },
                "note": note,
            }
        pull = client.get_pull_request(repository, pull_request)
        source_sha, source_repository, base_ref = _repair_source(pull)
        if expected_source_sha and source_sha != expected_source_sha:
            raise ClientInputError("pull request head changed; run a new review before fixing")
        suffix = (
            hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:16]
            if operation_key
            else datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        )
        branch = "evoagent/fix-pr-%d-%s" % (pull_request, suffix)
        allowed = set(self._rules) if allowed_rule_ids is None else set(allowed_rule_ids)
        unknown = allowed.difference(self._rules)
        if unknown:
            raise ValueError("unknown automatic repair rules: %s" % ", ".join(sorted(unknown)))
        planned: list[tuple[str, FixResult]] = []
        by_path: dict[str, list[dict[str, Any]]] = {}
        for finding in report.get("findings", []):
            if finding.get("rule_id") in allowed:
                by_path.setdefault(finding["path"], []).append(finding)
        source_bytes = 0
        for path, findings in by_path.items():
            current = client.get_file(source_repository, path, source_sha)
            content = current.get("decoded_content") if isinstance(current, dict) else None
            if not isinstance(content, str):
                raise RuntimeError("code host returned an invalid repair source file")
            try:
                source_bytes += len(path.encode("utf-8")) + len(content.encode("utf-8"))
            except UnicodeEncodeError:
                raise RuntimeError("code host returned an invalid repair source file") from None
            if source_bytes > self.max_source_bytes:
                return {
                    "branch": None,
                    "source_sha": source_sha,
                    "commits": [],
                    "note": "Repair was skipped because source files exceeded the analysis budget.",
                }
            result = self.apply(content, findings, path)
            if not result["rules"]:
                continue
            planned.append((path, result))
        if not planned:
            return {
                "branch": None,
                "source_sha": source_sha,
                "commits": [],
                "note": "No finding was eligible for deterministic automatic repair.",
            }
        verification = self.verifier.verify_contents(
            {path: result["content"] for path, result in planned}
        )
        files = {path: result["content"] for path, result in planned}
        if verification["passed"] and self.verifier.test_command:
            archive = client.download_archive(source_repository, source_sha)
            verification = self.verifier.verify_archive(archive, files)
        if not verification["passed"]:
            return {
                "branch": None,
                "source_sha": source_sha,
                "commits": [],
                "verification": verification,
                "note": "Repair was blocked because compilation or tests failed.",
            }
        existing_draft = (
            client.find_pull_request_by_head(repository, branch, base_ref)
            if operation_key
            else None
        )
        existing_branch = client.get_branch(repository, branch) if operation_key else None
        if existing_draft is not None:
            _repair_draft_receipt(existing_draft, repository, branch, base_ref)
        applied_rules = tuple(
            sorted({rule for _path, result in planned for rule in result["rules"]})
        )

        def authorize_write() -> None:
            if _repair_source(client.get_pull_request(repository, pull_request)) != (
                source_sha,
                source_repository,
                base_ref,
            ):
                raise ClientInputError("pull request head changed; run a new review before fixing")
            if before_publish is not None:
                # The GitHub adapter invokes this immediately before every write attempt.
                before_publish(applied_rules)

        existing_sha = ""
        if existing_branch is not None:
            branch_object = (
                existing_branch.get("object") if isinstance(existing_branch, dict) else None
            )
            candidate_sha = branch_object.get("sha") if isinstance(branch_object, dict) else None
            if not isinstance(candidate_sha, str) or not candidate_sha:
                raise RuntimeError("code host returned an invalid repair branch")
            existing_sha = candidate_sha
        commit = client.create_atomic_commit(
            repository,
            branch,
            source_sha,
            files,
            "fix: apply verified EvoAgent repairs for PR #%d" % pull_request,
            existing_sha=existing_sha,
            before_write=authorize_write,
        )
        commit_sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(commit_sha, str) or not commit_sha:
            raise RuntimeError("code host returned an invalid repair commit")
        if existing_draft:
            draft = existing_draft
        else:
            draft = client.create_draft_pull_request(
                repository,
                "fix: verified EvoAgent repairs for #%d" % pull_request,
                branch,
                base_ref,
                "Automated deterministic repair. All configured compile and test gates passed.",
                before_write=authorize_write,
            )
        draft_number, draft_url = _repair_draft_receipt(draft, repository, branch, base_ref)
        commits = [
            {
                "paths": sorted(files),
                "rules": list(applied_rules),
                "sha": commit_sha,
            }
        ]
        return {
            "branch": branch,
            "source_sha": source_sha,
            "commits": commits,
            "draft_pull_request": {"number": draft_number, "url": draft_url},
            "verification": verification,
            "note": (
                "The previously published verified repair was reused."
                if existing_draft
                else "Verified repairs were published as one atomic commit in a draft pull request."
            ),
        }
