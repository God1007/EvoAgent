import ast
import unittest

from evoagent.errors import AccessDeniedError, ClientInputError
from evoagent.fix_rules import RuleMutation
from evoagent.fixer import SafeFixer
from evoagent.models import MAX_RULE_ID_CHARS


class _NamedRule:
    def __init__(self, rule_id):
        self.rule_id = rule_id

    def apply_python(self, _tree, _target_lines):
        return RuleMutation(False)

    def propose_line(self, line):
        return line


class FixRuleRegistrationTests(unittest.TestCase):
    def test_rule_ids_are_bounded_stable_tokens(self):
        for rule_id in (None, [], "", " leading", "two words", "x" * (MAX_RULE_ID_CHARS + 1)):
            with self.subTest(rule_id=rule_id), self.assertRaisesRegex(ValueError, "rule id"):
                SafeFixer(rules=[_NamedRule(rule_id)])


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
        self.pull_calls = 0
        self.commit_calls = 0
        self.pr_calls = 0
        self.existing_branch = None
        self.existing_pr = None
        self.file_refs = []
        self.validated_reuses = 0

    def get_pull_request(self, repo, number):
        self.pull_calls += 1
        return {
            "state": "open",
            "draft": False,
            "head": {"ref": "feature", "sha": "abc123", "repo": {"full_name": repo}},
            "base": {"ref": "main"},
        }

    def get_file(self, repo, path, ref):
        self.file_refs.append(ref)
        return {"decoded_content": 'token = "abcdef"\n', "sha": "filesha"}

    def download_archive(self, repo, sha):
        return b"zip-bytes"

    def create_atomic_commit(
        self, repo, branch, parent, files, message, existing_sha="", before_write=None
    ):
        if before_write is not None:
            before_write()
        if existing_sha:
            if existing_sha != "newsha" or files != self.created_commit["files"]:
                raise RuntimeError("existing repair branch does not match the verified repair")
            self.validated_reuses += 1
            return {"sha": existing_sha}
        self.commit_calls += 1
        self.created_commit = {"branch": branch, "files": files}
        self.existing_branch = {"object": {"sha": "newsha"}}
        return {"sha": "newsha"}

    def create_draft_pull_request(self, repo, title, head, base, body, before_write=None):
        if before_write is not None:
            before_write()
        self.pr_calls += 1
        self.created_pr = {"title": title, "head": head}
        self.existing_pr = {
            "number": 7,
            "html_url": "https://github.com/o/r/pull/7",
            "state": "open",
            "draft": True,
            "head": {"ref": head, "repo": {"full_name": repo}},
            "base": {"ref": base, "repo": {"full_name": repo}},
        }
        return self.existing_pr

    def get_branch(self, repo, branch):
        return self.existing_branch

    def find_pull_request_by_head(self, repo, branch, base):
        return self.existing_pr


def _report():
    return {"findings": [{"path": "a.py", "line": 1, "rule_id": "SEC-HARDCODED-SECRET"}]}


class CreateFixCommitsTests(unittest.TestCase):
    def test_missing_pull_source_metadata_blocks_reads_and_writes(self):
        for field in ("repo", "base"):
            client = _FakeClient()
            original = client.get_pull_request

            def malformed_pull(repo, number, _field=field, _original=original):
                pull = _original(repo, number)
                if _field == "repo":
                    pull["head"]["repo"] = None
                else:
                    pull.pop("base")
                return pull

            client.get_pull_request = malformed_pull
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RuntimeError, "invalid pull request source metadata"),
            ):
                SafeFixer(verifier=_FakeVerifier()).create_fix_commits(client, "o/r", 1, _report())
            self.assertEqual([], client.file_refs)
            self.assertEqual(0, client.commit_calls)
            self.assertEqual(0, client.pr_calls)

    def test_invalid_or_oversized_repair_source_never_reaches_ast_or_publication(self):
        client = _FakeClient()
        client.get_file = lambda *_args: {"decoded_content": 7}
        with self.assertRaisesRegex(RuntimeError, "invalid repair source file"):
            SafeFixer(verifier=_FakeVerifier()).create_fix_commits(client, "o/r", 1, _report())

        client = _FakeClient()
        result = SafeFixer(verifier=_FakeVerifier(), max_source_bytes=5).create_fix_commits(
            client, "o/r", 1, _report()
        )
        self.assertIsNone(result["branch"])
        self.assertIn("analysis budget", result["note"])
        self.assertEqual(0, client.commit_calls)
        self.assertEqual(0, client.pr_calls)

    def test_pr_closed_or_updated_during_verification_blocks_github_writes(self):
        for mutation, error in (
            ("state", "no longer open"),
            ("draft", "is a draft"),
            ("head", "head changed"),
            ("base", "head changed"),
        ):
            client = _FakeClient()
            original = client.get_pull_request
            calls = 0

            def changing_pull(repo, number, _original=original, _mutation=mutation):
                nonlocal calls
                calls += 1
                pull = _original(repo, number)
                if calls > 1:
                    if _mutation == "state":
                        pull["state"] = "closed"
                    elif _mutation == "draft":
                        pull["draft"] = True
                    elif _mutation == "base":
                        pull["base"]["ref"] = "release"
                    else:
                        pull["head"]["sha"] = "updated-sha"
                return pull

            client.get_pull_request = changing_pull
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ClientInputError, error):
                SafeFixer(verifier=_FakeVerifier()).create_fix_commits(
                    client,
                    "o/r",
                    1,
                    _report(),
                )
            self.assertEqual(0, client.commit_calls)
            self.assertEqual(0, client.pr_calls)

    def test_policy_recheck_can_stop_verified_repair_before_github_writes(self):
        client = _FakeClient()

        def disabled_after_verification(applied_rules):
            self.assertEqual(("SEC-HARDCODED-SECRET",), applied_rules)
            raise AccessDeniedError("automatic repair was disabled")

        with self.assertRaisesRegex(AccessDeniedError, "disabled"):
            SafeFixer(verifier=_FakeVerifier()).create_fix_commits(
                client,
                "o/r",
                1,
                _report(),
                before_publish=disabled_after_verification,
            )

        self.assertEqual(0, client.commit_calls)
        self.assertEqual(0, client.pr_calls)

    def test_no_eligible_finding_returns_note(self):
        fixer = SafeFixer(verifier=_FakeVerifier())
        report = {"findings": [{"path": "a.py", "line": 1, "rule_id": "SEC-EVAL"}]}
        result = fixer.create_fix_commits(_FakeClient(), "o/r", 1, report)
        self.assertIsNone(result["branch"])
        self.assertIn("No finding was eligible", result["note"])

    def test_policy_rule_allowlist_filters_other_deterministic_repairs(self):
        fixer = SafeFixer(verifier=_FakeVerifier())
        result = fixer.create_fix_commits(
            _FakeClient(),
            "o/r",
            1,
            _report(),
            allowed_rule_ids=["REL-DEBUG-PRINT"],
        )
        self.assertIsNone(result["branch"])
        self.assertEqual([], result["commits"])

    def test_policy_rule_allowlist_rejects_unavailable_rule(self):
        fixer = SafeFixer(verifier=_FakeVerifier())
        with self.assertRaisesRegex(ValueError, "unknown automatic repair rules"):
            fixer.create_fix_commits(
                _FakeClient(), "o/r", 1, _report(), allowed_rule_ids=["UNKNOWN"]
            )

    def test_verification_failure_blocks_repair(self):
        fixer = SafeFixer(verifier=_FakeVerifier(contents_pass=False))
        result = fixer.create_fix_commits(_FakeClient(), "o/r", 1, _report())
        self.assertIsNone(result["branch"])
        self.assertIn("blocked", result["note"])

    def test_successful_repair_opens_draft_pr(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier())
        result = fixer.create_fix_commits(client, "o/r", 1, _report(), expected_source_sha="abc123")
        self.assertTrue(result["branch"].startswith("evoagent/fix-pr-1-"))
        self.assertEqual(7, result["draft_pull_request"]["number"])
        self.assertEqual(["SEC-HARDCODED-SECRET"], result["commits"][0]["rules"])
        self.assertIsNotNone(client.created_commit)
        self.assertEqual(["abc123"], client.file_refs)

    def test_new_repair_refuses_a_non_draft_provider_response(self):
        client = _FakeClient()
        create_draft = client.create_draft_pull_request

        def create_ready(*args, **kwargs):
            result = create_draft(*args, **kwargs)
            result["draft"] = False
            return result

        client.create_draft_pull_request = create_ready
        with self.assertRaisesRegex(RuntimeError, "invalid draft pull request"):
            SafeFixer(verifier=_FakeVerifier()).create_fix_commits(client, "o/r", 1, _report())

        self.assertEqual(1, client.commit_calls)
        self.assertEqual(1, client.pr_calls)

    def test_incomplete_code_host_receipts_never_report_publication_success(self):
        fixer = SafeFixer(verifier=_FakeVerifier())
        client = _FakeClient()
        client.create_atomic_commit = lambda *_args, **_kwargs: {}

        with self.assertRaisesRegex(RuntimeError, "invalid repair commit"):
            fixer.create_fix_commits(client, "o/r", 1, _report())
        self.assertEqual(0, client.pr_calls)

        client = _FakeClient()
        client.create_draft_pull_request = lambda *_args, **_kwargs: {}
        with self.assertRaisesRegex(RuntimeError, "invalid draft pull request"):
            fixer.create_fix_commits(client, "o/r", 1, _report())

    def test_stale_review_head_is_rejected_before_reading_files(self):
        client = _FakeClient()

        with self.assertRaisesRegex(ClientInputError, "head changed"):
            SafeFixer(verifier=_FakeVerifier()).create_fix_commits(
                client, "o/r", 1, _report(), expected_source_sha="reviewed-sha"
            )

        self.assertEqual([], client.file_refs)
        self.assertEqual(0, client.commit_calls)

    def test_publication_is_blocked_when_no_test_command(self):
        for command in ("", " \t "):
            client = _FakeClient()
            fixer = SafeFixer(verifier=_FakeVerifier(test_command=command))

            with self.subTest(command=command):
                result = fixer.create_fix_commits(client, "o/r", 1, _report())
                self.assertIsNone(result["branch"])
                self.assertFalse(result["verification"]["passed"])
                self.assertEqual("skipped", result["verification"]["status"])
                self.assertEqual(0, client.pull_calls)
                self.assertEqual([], client.file_refs)
                self.assertEqual(0, client.commit_calls)
                self.assertEqual(0, client.pr_calls)

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
        self.assertEqual(1, client.validated_reuses)
        self.assertIn("reused", second["note"])

    def test_operation_key_rejects_a_tampered_existing_branch(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier())
        fixer.create_fix_commits(client, "o/r", 1, _report(), operation_key="fix-pr:tenant:task")
        client.created_commit["files"]["a.py"] = "tampered"

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            fixer.create_fix_commits(
                client, "o/r", 1, _report(), operation_key="fix-pr:tenant:task"
            )

        self.assertEqual(1, client.commit_calls)
        self.assertEqual(1, client.pr_calls)

    def test_operation_key_rejects_a_non_draft_existing_pull_request(self):
        client = _FakeClient()
        fixer = SafeFixer(verifier=_FakeVerifier())
        fixer.create_fix_commits(client, "o/r", 1, _report(), operation_key="fix-pr:tenant:task")
        client.existing_pr["draft"] = False

        with self.assertRaisesRegex(RuntimeError, "pull request does not match"):
            fixer.create_fix_commits(
                client, "o/r", 1, _report(), operation_key="fix-pr:tenant:task"
            )

        self.assertEqual(1, client.commit_calls)
        self.assertEqual(1, client.pr_calls)


if __name__ == "__main__":
    unittest.main()
