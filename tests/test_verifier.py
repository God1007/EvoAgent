import io
import os
import sys
import tempfile
import time
import unittest
import zipfile

from evoagent import verifier as verifier_module
from evoagent.verifier import RepairVerifier


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


class VerifierContentsTests(unittest.TestCase):
    def test_syntax_error_fails_compile_gate(self):
        result = RepairVerifier().verify_contents({"app.py": "def broken(:\n"})
        self.assertFalse(result["passed"])

    def test_clean_python_passes_compile_gate(self):
        result = RepairVerifier().verify_contents({"app.py": "x = 1\n"})
        self.assertTrue(result["passed"])
        self.assertEqual(1, len(result["checks"]))

    def test_missing_worktree_raises(self):
        with self.assertRaises(ValueError):
            RepairVerifier(test_command="pytest").verify_worktree("/no/such/dir/xyz")


class VerifierArchiveTests(unittest.TestCase):
    def test_archive_applies_files_and_passes_without_test_command(self):
        archive = _zip({"repo-main/a.py": "old = 1\n", "repo-main/keep.py": "y = 2\n"})
        result = RepairVerifier().verify_archive(archive, {"a.py": "new = 1\n"})
        self.assertTrue(result["passed"])

    def test_archive_compile_failure_short_circuits(self):
        archive = _zip({"repo-main/a.py": "x = 1\n"})
        result = RepairVerifier().verify_archive(archive, {"a.py": "def broken(:\n"})
        self.assertFalse(result["passed"])

    def test_archive_rejects_zip_slip_member(self):
        archive = _zip({"../evil.py": "x = 1\n"})
        with self.assertRaises(ValueError):
            RepairVerifier().verify_archive(archive, {})

    def test_repair_path_cannot_escape_repository(self):
        archive = _zip({"repo-main/a.py": "x = 1\n"})
        with self.assertRaises(ValueError):
            RepairVerifier().verify_archive(archive, {"../escape.py": "x = 1\n"})

    def test_no_command_is_a_pass_with_note(self):
        with tempfile.TemporaryDirectory() as root:
            result = RepairVerifier().verify_worktree(root)
        self.assertTrue(result["passed"])
        self.assertIn("No repository test command", result["note"])
        self.assertIn("duration_seconds", result)

    def test_require_container_refuses_host_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(test_command="pytest", require_container=True)
            result = verifier.verify_worktree(root)
        self.assertFalse(result["passed"])
        self.assertIn("container isolation is required", result["checks"][0]["detail"])

    def test_missing_binary_fails_gate_without_crashing(self):
        verifier = RepairVerifier(timeout_seconds=5)
        returncode, detail, timed_out = verifier._execute(
            ["evoagent-nonexistent-binary-xyz"], cwd=None, env=None
        )
        self.assertIsNone(returncode)
        self.assertFalse(timed_out)
        self.assertIn("failed to launch", detail)


@unittest.skipIf(os.name == "nt", "POSIX host-isolation behaviour")
class VerifierHostModeTests(unittest.TestCase):
    def test_successful_command_passes(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(
                test_command='%s -c "import sys; sys.exit(0)"' % sys.executable,
                timeout_seconds=30,
            )
            result = verifier.verify_worktree(root)
        self.assertTrue(result["passed"])

    def test_failing_command_fails(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(
                test_command='%s -c "import sys; sys.exit(3)"' % sys.executable,
                timeout_seconds=30,
            )
            result = verifier.verify_worktree(root)
        self.assertFalse(result["passed"])

    def test_verbose_but_passing_command_is_not_killed(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(
                test_command="yes evoagent | head -c 5000000",
                timeout_seconds=30,
                max_output_bytes=4096,
            )
            result = verifier.verify_worktree(root)
        self.assertTrue(result["passed"])
        self.assertLessEqual(len(result["checks"][0]["detail"]), 4096)

    def test_retained_detail_is_the_tail_of_output(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(
                test_command="printf 'A%.0s' $(seq 1 100000); echo END_MARKER; exit 0",
                timeout_seconds=30,
                max_output_bytes=64,
            )
            result = verifier.verify_worktree(root)
        self.assertTrue(result["passed"])
        self.assertIn("END_MARKER", result["checks"][0]["detail"])
        self.assertLessEqual(len(result["checks"][0]["detail"]), 64)

    def test_timeout_kills_background_child_in_process_group(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(
                test_command="sleep 30 & echo $! > child.pid; wait",
                timeout_seconds=1,
            )
            started = time.monotonic()
            result = verifier.verify_worktree(root)
            elapsed = time.monotonic() - started
            pid = int((open(os.path.join(root, "child.pid")).read() or "0").strip())
        self.assertFalse(result["passed"])
        self.assertIn("exceeded", result["checks"][0]["detail"])
        self.assertLess(elapsed, 15)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class VerifierContainerModeTests(unittest.TestCase):
    def _capture_docker_command(self, verifier: RepairVerifier):
        captured = {}

        def _fake_run(command, **kwargs):
            captured["command"] = command

            class _Result:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return _Result()

        # `_execute` uses Popen; patch it to capture the docker argv instead of running it.
        def _fake_execute(command, cwd, env):
            captured["command"] = command
            return 0, "ok", False

        original = verifier._execute
        verifier._execute = _fake_execute  # type: ignore[method-assign]
        try:
            with tempfile.TemporaryDirectory() as root:
                result = verifier.verify_worktree(root)
        finally:
            verifier._execute = original  # type: ignore[method-assign]
        return result, captured["command"]

    def test_container_command_uses_paired_isolation_flags(self):
        verifier = RepairVerifier(
            test_command="pytest -q",
            container_image="python:3.12-slim",
            memory_mb=512,
            pids_limit=64,
            cpus=0.5,
        )
        result, command = self._capture_docker_command(verifier)
        self.assertTrue(result["passed"])
        self.assertEqual("docker", command[0])
        self.assertEqual("512m", command[command.index("--memory") + 1])
        self.assertEqual("64", command[command.index("--pids-limit") + 1])
        self.assertEqual("0.5", command[command.index("--cpus") + 1])
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertIn("--read-only", command)
        self.assertEqual("ALL", command[command.index("--cap-drop") + 1])
        self.assertIn("no-new-privileges", command)
        if os.name != "nt":
            self.assertIn("--user", command)
        self.assertIn("python:3.12-slim", command)
        self.assertEqual(["sh", "-c", "pytest -q"], command[-3:])

    def test_container_timeout_force_removes_container(self):
        calls = []
        verifier = RepairVerifier(test_command="pytest", container_image="img", timeout_seconds=1)

        def _fake_execute(command, cwd, env):
            calls.append(command)
            return None, "", True

        def _fake_run(command, **kwargs):
            calls.append(command)

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        original_run = verifier_module.subprocess.run
        verifier._execute = _fake_execute  # type: ignore[method-assign]
        verifier_module.subprocess.run = _fake_run  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as root:
                result = verifier.verify_worktree(root)
        finally:
            verifier_module.subprocess.run = original_run

        self.assertFalse(result["passed"])
        self.assertIn("exceeded", result["checks"][0]["detail"])
        self.assertTrue(any(c[:3] == ["docker", "rm", "-f"] for c in calls))


if __name__ == "__main__":
    unittest.main()
