import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from evoagent import verifier as verifier_module
from evoagent.bootstrap import _repair_verifier, build_components
from evoagent.metrics import Metrics
from evoagent.verifier import RepairVerifier


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return buffer.getvalue()


class VerifierContentsTests(unittest.TestCase):
    def test_resource_limits_cannot_be_disabled(self):
        for name, value in (
            ("timeout_seconds", 0),
            ("memory_mb", 0),
            ("pids_limit", 0),
            ("max_output_bytes", 0),
            ("max_file_mb", 0),
            ("timeout_seconds", float("nan")),
            ("memory_mb", True),
            ("cpus", 0),
            ("cpus", float("nan")),
            ("cpus", True),
            ("cpus", "1"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                RepairVerifier(**{name: value})

    def test_application_verifier_always_requires_a_container(self):
        settings = SimpleNamespace(
            repair_verify_timeout_seconds=10,
            repair_container_image="",
            repair_memory_mb=256,
            repair_pids_limit=32,
            repair_cpus=0.5,
            repair_max_output_bytes=1024,
        )

        verifier = _repair_verifier(settings, "pytest", "sha256:" + "a" * 64)
        self.assertTrue(verifier.require_container)
        self.assertEqual("sha256:" + "a" * 64, verifier.container_image)

    def test_application_resolves_repair_image_before_opening_the_store(self):
        settings = SimpleNamespace(repair_container_image="missing")
        with (
            patch("evoagent.bootstrap.resolve_container_image", side_effect=ValueError("missing")),
            patch("evoagent.bootstrap.create_store") as create_store,
            self.assertRaisesRegex(ValueError, "missing"),
        ):
            build_components(settings)  # type: ignore[arg-type]

        create_store.assert_not_called()

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

    def test_archive_preserves_test_attestation(self):
        archive = _zip({"repo-main/a.py": "old = 1\n"})
        verifier = RepairVerifier(test_command="pytest")
        verifier.verify_worktree = lambda _root: {  # type: ignore[method-assign]
            "passed": True,
            "checks": [],
            "attestation": {"test_command_sha256": "digest"},
        }

        result = verifier.verify_archive(archive, {"a.py": "new = 1\n"})

        self.assertEqual({"test_command_sha256": "digest"}, result["attestation"])

    def test_archive_compile_failure_short_circuits(self):
        archive = _zip({"repo-main/a.py": "x = 1\n"})
        result = RepairVerifier().verify_archive(archive, {"a.py": "def broken(:\n"})
        self.assertFalse(result["passed"])

    def test_archive_rejects_zip_slip_member(self):
        archive = _zip({"../evil.py": "x = 1\n"})
        with self.assertRaises(ValueError):
            RepairVerifier().verify_archive(archive, {})

    def test_archive_rejects_excessive_uncompressed_size(self):
        archive = _zip({"repo-main/a.py": "12345"})
        with (
            patch.object(RepairVerifier, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 4),
            self.assertRaisesRegex(ValueError, "extraction limits"),
        ):
            RepairVerifier().verify_archive(archive, {})

    def test_archive_expansion_cannot_exceed_container_memory_budget(self):
        archive = _zip({"repo-main/a.py": "x" * (1024 * 1024 + 1)})
        with self.assertRaisesRegex(ValueError, "extraction limits"):
            RepairVerifier(memory_mb=1).verify_archive(archive, {})

    def test_archive_type_and_compressed_size_share_the_memory_budget(self):
        verifier = RepairVerifier(memory_mb=1)
        for archive in ("not-bytes", b"x" * (1024 * 1024 + 1)):
            with (
                self.subTest(type=type(archive).__name__),
                self.assertRaisesRegex(ValueError, "extraction limits"),
            ):
                verifier.verify_archive(archive, {})  # type: ignore[arg-type]

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
        self.assertRegex(
            detail,
            r"^verification launch failed \[type=builtins\.FileNotFoundError; "
            r"ref=[0-9a-f]{16}\]$",
        )
        self.assertNotIn("evoagent-nonexistent-binary-xyz", detail)


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
    def test_container_cleanup_failure_is_not_ignored(self):
        verifier = RepairVerifier(test_command="pytest", container_image="img")
        verifier._execute = lambda *_args, **_kwargs: (0, "", False)  # type: ignore[method-assign]

        def run(command, **_kwargs):
            return SimpleNamespace(returncode=1 if command[:3] == ["docker", "rm", "-f"] else 0)

        captured = Metrics()
        with (
            patch.object(verifier_module.subprocess, "run", side_effect=run),
            patch("evoagent.verifier.metrics", captured),
            tempfile.TemporaryDirectory() as root,
            self.assertRaisesRegex(RuntimeError, "cleanup failed"),
        ):
            verifier.verify_worktree(root)

        self.assertIn("evoagent_repair_container_cleanup_failures_total 1.0", captured.prometheus())

    def _capture_docker_command(self, verifier: RepairVerifier):
        captured = {"commands": []}

        def _fake_run(command, **kwargs):
            captured["commands"].append(command)

            class _Result:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return _Result()

        # `_execute` uses Popen; patch it to capture the docker argv instead of running it.
        def _fake_execute(command, cwd, env, timeout_seconds=None):
            captured["commands"].append(command)
            return 0, "ok", False

        original = verifier._execute
        verifier._execute = _fake_execute  # type: ignore[method-assign]
        try:
            with (
                patch.object(verifier_module.subprocess, "run", side_effect=_fake_run),
                tempfile.TemporaryDirectory() as root,
            ):
                result = verifier.verify_worktree(root)
        finally:
            verifier._execute = original  # type: ignore[method-assign]
        return result, captured["commands"]

    def test_container_command_uses_paired_isolation_flags(self):
        verifier = RepairVerifier(
            test_command="pytest -q",
            container_image="python:3.12-slim",
            memory_mb=512,
            pids_limit=64,
            cpus=0.5,
        )
        result, commands = self._capture_docker_command(verifier)
        command, copy, execute = commands[:3]
        self.assertTrue(result["passed"])
        self.assertEqual("docker", command[0])
        self.assertEqual("512m", command[command.index("--memory") + 1])
        self.assertEqual("64", command[command.index("--pids-limit") + 1])
        self.assertEqual("0.5", command[command.index("--cpus") + 1])
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertEqual("never", command[command.index("--pull") + 1])
        self.assertIn("--read-only", command)
        self.assertEqual("ALL", command[command.index("--cap-drop") + 1])
        self.assertIn("no-new-privileges", command)
        if os.name != "nt":
            self.assertIn("--user", command)
        self.assertIn("python:3.12-slim", command)
        self.assertNotIn("-v", command)
        self.assertTrue(command[command.index("--mount") + 1].endswith(",dst=/source,readonly"))
        self.assertEqual(["docker", "exec", "--workdir", "/work"], copy[:4])
        self.assertEqual(["sh", "-c", "cp -R /source/. /work"], copy[-3:])
        self.assertEqual(["sh", "-c", "pytest -q"], execute[-3:])
        self.assertEqual(
            {
                "execution_mode": "container",
                "container_image": "python:3.12-slim",
                "test_command_sha256": hashlib.sha256(b"pytest -q").hexdigest(),
            },
            result["attestation"],
        )
        self.assertNotIn("pytest -q", str(result["attestation"]))

    def test_container_timeout_force_removes_container(self):
        calls = []
        verifier = RepairVerifier(test_command="pytest", container_image="img", timeout_seconds=1)

        def _fake_execute(command, cwd, env, timeout_seconds=None):
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

    def test_copy_failure_removes_container_without_running_tests(self):
        calls = []
        verifier = RepairVerifier(test_command="pytest", container_image="img")

        def run(command, **_kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=1 if "cp -R /source/. /work" in command else 0)

        def execute(*_args, **_kwargs):
            raise AssertionError("tests must not run after a failed worktree copy")

        verifier._execute = execute  # type: ignore[method-assign]
        with (
            patch.object(verifier_module.subprocess, "run", side_effect=run),
            tempfile.TemporaryDirectory() as root,
        ):
            result = verifier.verify_worktree(root)

        self.assertEqual("error", result["status"])
        self.assertTrue(any(command[:3] == ["docker", "rm", "-f"] for command in calls))


if __name__ == "__main__":
    unittest.main()
