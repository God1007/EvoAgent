import ast
import unittest

from evoagent.fixer import SafeFixer


class ProposeLineTests(unittest.TestCase):
    def test_debug_print_is_removed(self):
        self.assertEqual("", SafeFixer().propose_line("print(x)", {"rule_id": "REL-DEBUG-PRINT"}))

    def test_shell_true_is_flipped(self):
        self.assertEqual(
            "run(cmd, shell=False)",
            SafeFixer().propose_line("run(cmd, shell=True)", {"rule_id": "SEC-SUBPROCESS-SHELL"}),
        )

    def test_hardcoded_secret_reads_env(self):
        line = 'token = "abcdef"'
        self.assertEqual(
            'token = os.environ["TOKEN"]',
            SafeFixer().propose_line(line, {"rule_id": "SEC-HARDCODED-SECRET"}),
        )

    def test_secret_rule_without_assignment_is_untouched(self):
        line = "call_something()"
        self.assertEqual(line, SafeFixer().propose_line(line, {"rule_id": "SEC-HARDCODED-SECRET"}))

    def test_unknown_rule_is_untouched(self):
        self.assertEqual("keep()", SafeFixer().propose_line("keep()", {"rule_id": "OTHER"}))


class ApplyTests(unittest.TestCase):
    def test_non_python_file_uses_line_path(self):
        content = "run(cmd, shell=True)\n"
        result = SafeFixer().apply(content, [{"path": "a.sh", "line": 1, "rule_id": "X"}], "a.sh")
        # No supported rule maps for line 1 => unchanged content, no rules.
        self.assertEqual([], result["rules"])

    def test_line_path_flips_shell_in_shell_script(self):
        content = "run(cmd, shell=True)\n"
        result = SafeFixer().apply(
            content, [{"path": "a.sh", "line": 1, "rule_id": "SEC-SUBPROCESS-SHELL"}], "a.sh"
        )
        self.assertIn("shell=False", result["content"])
        self.assertEqual(["SEC-SUBPROCESS-SHELL"], result["rules"])

    def test_python_ast_path_rewrites_secret_and_adds_import(self):
        content = 'token = "abcdef"\n'
        result = SafeFixer().apply(
            content, [{"path": "a.py", "line": 1, "rule_id": "SEC-HARDCODED-SECRET"}], "a.py"
        )
        self.assertIn("import os", result["content"])
        # Assert AST semantics, not incidental unparse quote style.
        tree = ast.parse(result["content"])
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign))
        self.assertIsInstance(assignment.value, ast.Subscript)
        self.assertEqual("environ", assignment.value.value.attr)
        self.assertEqual("TOKEN", assignment.value.slice.value)
        self.assertEqual(["SEC-HARDCODED-SECRET"], result["rules"])

    def test_python_ast_path_removes_print_statement(self):
        content = "value = compute()\nprint(value)\n"
        result = SafeFixer().apply(
            content, [{"path": "a.py", "line": 2, "rule_id": "REL-DEBUG-PRINT"}], "a.py"
        )
        tree = ast.parse(result["content"])
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        self.assertEqual([], calls)
        self.assertEqual(["REL-DEBUG-PRINT"], result["rules"])

    def test_python_ast_path_flips_shell_true_keyword(self):
        content = "import subprocess\nsubprocess.run(cmd, shell=True)\n"
        result = SafeFixer().apply(
            content, [{"path": "a.py", "line": 2, "rule_id": "SEC-SUBPROCESS-SHELL"}], "a.py"
        )
        tree = ast.parse(result["content"])
        keyword = next(
            kw
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "shell"
        )
        self.assertIsInstance(keyword.value, ast.Constant)
        self.assertIs(False, keyword.value.value)
        self.assertEqual(["SEC-SUBPROCESS-SHELL"], result["rules"])

    def test_ast_path_only_touches_targeted_line(self):
        content = "print(keep_me)\nprint(remove_me)\n"
        result = SafeFixer().apply(
            content, [{"path": "a.py", "line": 2, "rule_id": "REL-DEBUG-PRINT"}], "a.py"
        )
        self.assertIn("keep_me", result["content"])
        self.assertNotIn("remove_me", result["content"])

    def test_python_syntax_error_falls_back_to_line_path(self):
        content = "def broken(:\nprint(x)\n"
        result = SafeFixer().apply(
            content, [{"path": "a.py", "line": 2, "rule_id": "REL-DEBUG-PRINT"}], "a.py"
        )
        # AST parse fails, line path drops the print line.
        self.assertNotIn("print(x)", result["content"])
        self.assertEqual(["REL-DEBUG-PRINT"], result["rules"])

    def test_out_of_range_line_is_ignored(self):
        content = "x = 1\n"
        result = SafeFixer().apply(
            content, [{"path": "a.sh", "line": 99, "rule_id": "REL-DEBUG-PRINT"}], "a.sh"
        )
        self.assertEqual([], result["rules"])


class _FakeVerifier:
    def __init__(self, contents_pass=True, archive_pass=True, test_command="pytest"):
        self.test_command = test_command
        self._contents_pass = contents_pass
        self._archive_pass = archive_pass

    def verify_contents(self, files):
        return {"passed": self._contents_pass, "checks": []}

    def verify_archive(self, archive, files):
        return {"passed": self._archive_pass, "checks": []}


class _FakeClient:
    def __init__(self):
        self.created_commit = None
        self.created_pr = None
        self.commit_calls = 0
        self.pr_calls = 0
        self.existing_branch = None
        self.existing_pr = None

    def get_pull_request(self, repo, number):
        return {
            "head": {"ref": "feature", "sha": "abc123", "repo": {"full_name": repo}},
            "base": {"ref": "main"},
        }

    def get_file(self, repo, path, ref):
        return {"decoded_content": 'token = "abcdef"\n', "sha": "filesha"}

    def download_archive(self, repo, sha):
        return b"zip-bytes"

    def create_atomic_commit(self, repo, branch, parent, files, message):
        self.commit_calls += 1
        self.created_commit = {"branch": branch, "files": files}
        self.existing_branch = {"object": {"sha": "newsha"}}
        return {"sha": "newsha"}

    def create_draft_pull_request(self, repo, title, head, base, body):
        self.pr_calls += 1
        self.created_pr = {"title": title, "head": head}
        self.existing_pr = {"number": 7, "html_url": "https://github.com/o/r/pull/7"}
        return self.existing_pr

    def get_branch(self, repo, branch):
        return self.existing_branch

    def find_pull_request_by_head(self, repo, branch):
        return self.existing_pr


def _report():
    return {"findings": [{"path": "a.py", "line": 1, "rule_id": "SEC-HARDCODED-SECRET"}]}


class CreateFixCommitsTests(unittest.TestCase):
    def test_no_eligible_finding_returns_note(self):
        fixer = SafeFixer(verifier=_FakeVerifier())
        report = {"findings": [{"path": "a.py", "line": 1, "rule_id": "SEC-EVAL"}]}
        result = fixer.create_fix_commits(_FakeClient(), "o/r", 1, report)
        self.assertIsNone(result["branch"])
        self.assertIn("No finding was eligible", result["note"])

    def test_verification_failure_blocks_repair(self):
        fixer = SafeFixer(verifier=_FakeVerifier(contents_pass=False))
        result = fixer.create_fix_commits(_FakeClient(), "o/r", 1, _report())
        self.assertIsNone(result["branch"])
        self.assertIn("blocked", result["note"])

    def test_successful_repair_opens_draft_pr(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier())
        result = fixer.create_fix_commits(client, "o/r", 1, _report())
        self.assertTrue(result["branch"].startswith("evoagent/fix-pr-1-"))
        self.assertEqual(7, result["draft_pull_request"]["number"])
        self.assertEqual(["SEC-HARDCODED-SECRET"], result["commits"][0]["rules"])
        self.assertIsNotNone(client.created_commit)

    def test_archive_gate_skipped_when_no_test_command(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier(test_command=""))
        result = fixer.create_fix_commits(client, "o/r", 1, _report())
        self.assertTrue(result["branch"])

    def test_successful_repair_commits_the_fixed_content(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier())
        fixer.create_fix_commits(client, "o/r", 1, _report())
        committed = client.created_commit["files"]["a.py"]
        self.assertIn("os.environ", committed)
        self.assertNotIn('"abcdef"', committed)

    def test_operation_key_reuses_branch_and_draft_pull_request(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier())
        first = fixer.create_fix_commits(
            client, "o/r", 1, _report(), operation_key="fix-pr:tenant:task"
        )
        second = fixer.create_fix_commits(
            client, "o/r", 1, _report(), operation_key="fix-pr:tenant:task"
        )

        self.assertEqual(first["branch"], second["branch"])
        self.assertEqual(1, client.commit_calls)
        self.assertEqual(1, client.pr_calls)
        self.assertIn("reused", second["note"])


if __name__ == "__main__":
    unittest.main()
