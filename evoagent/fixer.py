import ast
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypedDict

from .fix_rules import FixRule, default_fix_rules
from .ports import CodeHostPort
from .verifier import RepairVerifier


class FixResult(TypedDict):
    content: str
    rules: list[str]


class SafeFixer:
    """Creates conservative fixes on a dedicated branch; never writes to the PR head."""

    def __init__(
        self,
        verifier: RepairVerifier | None = None,
        rules: Iterable[FixRule] | None = None,
    ):
        self.verifier = verifier or RepairVerifier()
        selected = default_fix_rules() if rules is None else list(rules)
        self._rules: dict[str, FixRule] = {}
        for rule in selected:
            if not isinstance(rule, FixRule):
                raise TypeError("repair rule does not implement the FixRule protocol")
            if rule.rule_id in self._rules:
                raise ValueError("duplicate repair rule id: %s" % rule.rule_id)
            self._rules[rule.rule_id] = rule

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
            isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (isinstance(node, ast.Import) and any(alias.name == module for alias in node.names))
                or (isinstance(node, ast.ImportFrom) and node.module == module)
            )
            for node in tree.body
        )

    def create_fix_commits(
        self,
        client: CodeHostPort,
        repository: str,
        pull_request: int,
        report: dict[str, Any],
    ) -> dict[str, Any]:
        pull = client.get_pull_request(repository, pull_request)
        source_ref = pull["head"]["ref"]
        source_sha = pull["head"]["sha"]
        source_repository = pull["head"].get("repo", {}).get("full_name") or repository
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        branch = "evoagent/fix-pr-%d-%s" % (pull_request, stamp)
        planned: list[tuple[str, dict[str, Any], FixResult]] = []
        by_path: dict[str, list[dict[str, Any]]] = {}
        for finding in report.get("findings", []):
            by_path.setdefault(finding["path"], []).append(finding)
        for path, findings in by_path.items():
            current = client.get_file(source_repository, path, source_ref)
            result = self.apply(current["decoded_content"], findings, path)
            if not result["rules"]:
                continue
            planned.append((path, current, result))
        if not planned:
            return {
                "branch": None,
                "source_sha": source_sha,
                "commits": [],
                "note": "No finding was eligible for deterministic automatic repair.",
            }
        verification = self.verifier.verify_contents(
            {path: result["content"] for path, _current, result in planned}
        )
        files = {path: result["content"] for path, _current, result in planned}
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
        commit = client.create_atomic_commit(
            repository,
            branch,
            source_sha,
            files,
            "fix: apply verified EvoAgent repairs for PR #%d" % pull_request,
        )
        draft = client.create_draft_pull_request(
            repository,
            "fix: verified EvoAgent repairs for #%d" % pull_request,
            branch,
            pull.get("base", {}).get("ref", "main"),
            "Automated deterministic repair. All configured compile and test gates passed.",
        )
        commits = [
            {
                "paths": sorted(files),
                "rules": sorted(
                    {rule for _path, _current, result in planned for rule in result["rules"]}
                ),
                "sha": commit.get("sha"),
            }
        ]
        return {
            "branch": branch,
            "source_sha": source_sha,
            "commits": commits,
            "draft_pull_request": {"number": draft.get("number"), "url": draft.get("html_url")},
            "verification": verification,
            "note": "Verified repairs were published as one atomic commit in a draft pull request.",
        }
