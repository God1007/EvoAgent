import ast
import re
from datetime import datetime, timezone
from typing import Dict, List

from .github import GitHubClient
from .verifier import RepairVerifier


class SafeFixer:
    """Creates conservative fixes on a dedicated branch; never writes to the PR head."""

    def propose_line(self, line: str, finding: dict) -> str:
        rule = finding.get("rule_id")
        if rule == "REL-DEBUG-PRINT":
            return ""
        if rule == "SEC-SUBPROCESS-SHELL":
            return re.sub(r"shell\s*=\s*True", "shell=False", line)
        if rule == "SEC-HARDCODED-SECRET":
            match = re.match(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"]).+?\3\s*$", line)
            if match:
                return '%s%s = os.environ["%s"]' % (match.group(1), match.group(2), match.group(2).upper())
        return line

    def apply(self, content: str, findings: List[dict], path: str) -> Dict[str, object]:
        if path.endswith(".py") and hasattr(ast, "unparse"):
            structured = self._apply_python_ast(content, findings, path)
            if structured["rules"]:
                return structured
        lines = content.splitlines()
        changed = []
        needs_os = False
        for finding in sorted((x for x in findings if x.get("path") == path), key=lambda x: x.get("line", 0), reverse=True):
            index = int(finding.get("line", 0)) - 1
            if index < 0 or index >= len(lines):
                continue
            replacement = self.propose_line(lines[index], finding)
            if replacement != lines[index]:
                needs_os = needs_os or "os.environ" in replacement
                lines[index] = replacement
                changed.append(finding.get("rule_id"))
        if needs_os and not any(re.match(r"\s*(import os|from os import)", line) for line in lines):
            lines.insert(0, "import os")
        return {"content": "\n".join(lines) + ("\n" if content.endswith("\n") else ""), "rules": changed}

    def _apply_python_ast(
        self, content: str, findings: List[dict], path: str,
    ) -> Dict[str, object]:
        targets = {
            (int(item.get("line", 0)), item.get("rule_id"))
            for item in findings if item.get("path") == path
        }
        changed = []
        needs_os = False

        class Transformer(ast.NodeTransformer):
            def visit_Expr(inner, node):
                if (
                    (node.lineno, "REL-DEBUG-PRINT") in targets
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "print"
                ):
                    changed.append("REL-DEBUG-PRINT")
                    return None
                return inner.generic_visit(node)

            def visit_Call(inner, node):
                node = inner.generic_visit(node)
                if (node.lineno, "SEC-SUBPROCESS-SHELL") in targets:
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "shell"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True
                        ):
                            keyword.value = ast.Constant(False)
                            changed.append("SEC-SUBPROCESS-SHELL")
                return node

            def visit_Assign(inner, node):
                nonlocal needs_os
                node = inner.generic_visit(node)
                if (
                    (node.lineno, "SEC-HARDCODED-SECRET") in targets
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    name = node.targets[0].id
                    node.value = ast.Subscript(
                        value=ast.Attribute(
                            value=ast.Name(id="os", ctx=ast.Load()),
                            attr="environ", ctx=ast.Load(),
                        ),
                        slice=ast.Constant(name.upper()), ctx=ast.Load(),
                    )
                    needs_os = True
                    changed.append("SEC-HARDCODED-SECRET")
                return node

        try:
            tree = Transformer().visit(ast.parse(content, filename=path))
        except SyntaxError:
            return {"content": content, "rules": []}
        if not changed:
            return {"content": content, "rules": []}
        if needs_os and not any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                (isinstance(node, ast.Import) and any(alias.name == "os" for alias in node.names))
                or (isinstance(node, ast.ImportFrom) and node.module == "os")
            )
            for node in tree.body
        ):
            tree.body.insert(0, ast.Import(names=[ast.alias(name="os")]))
        ast.fix_missing_locations(tree)
        value = ast.unparse(tree) + "\n"
        compile(value, path, "exec")
        return {"content": value, "rules": sorted(set(changed))}

    def __init__(self, verifier: RepairVerifier = None):
        self.verifier = verifier or RepairVerifier()

    def create_fix_commits(self, client: GitHubClient, repository: str, pull_request: int, report: dict) -> dict:
        pull = client.get_pull_request(repository, pull_request)
        source_ref = pull["head"]["ref"]
        source_sha = pull["head"]["sha"]
        source_repository = pull["head"].get("repo", {}).get("full_name") or repository
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        branch = "evoagent/fix-pr-%d-%s" % (pull_request, stamp)
        planned = []
        by_path = {}
        for finding in report.get("findings", []):
            by_path.setdefault(finding["path"], []).append(finding)
        for path, findings in by_path.items():
            current = client.get_file(source_repository, path, source_ref)
            result = self.apply(current["decoded_content"], findings, path)
            if not result["rules"]:
                continue
            planned.append((path, current, result))
        if not planned:
            return {"branch": None, "source_sha": source_sha, "commits": [],
                    "note": "No finding was eligible for deterministic automatic repair."}
        verification = self.verifier.verify_contents({
            path: result["content"] for path, _current, result in planned
        })
        files = {path: result["content"] for path, _current, result in planned}
        if verification["passed"] and self.verifier.test_command:
            archive = client.download_archive(source_repository, source_sha)
            verification = self.verifier.verify_archive(archive, files)
        if not verification["passed"]:
            return {
                "branch": None, "source_sha": source_sha, "commits": [],
                "verification": verification,
                "note": "Repair was blocked because compilation or tests failed.",
            }
        commit = client.create_atomic_commit(
            repository, branch, source_sha, files,
            "fix: apply verified EvoAgent repairs for PR #%d" % pull_request,
        )
        draft = client.create_draft_pull_request(
            repository,
            "fix: verified EvoAgent repairs for #%d" % pull_request,
            branch, pull.get("base", {}).get("ref", "main"),
            "Automated deterministic repair. All configured compile and test gates passed.",
        )
        commits = [{
            "paths": sorted(files),
            "rules": sorted({
                rule for _path, _current, result in planned for rule in result["rules"]
            }),
            "sha": commit.get("sha"),
        }]
        return {"branch": branch, "source_sha": source_sha, "commits": commits,
                "draft_pull_request": {
                    "number": draft.get("number"), "url": draft.get("html_url")
                },
                "verification": verification,
                "note": "Verified repairs were published as one atomic commit in a draft pull request."}
