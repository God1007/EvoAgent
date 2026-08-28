"""Real Docker execution checks, enabled only in the container CI job."""

import hashlib
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from evoagent.diff_parser import parse_unified_diff
from evoagent.skills import SandboxedSkillReviewer
from evoagent.verifier import RepairVerifier

CONTAINER_IMAGE = os.getenv("EVOAGENT_TEST_CONTAINER_IMAGE", "")


@unittest.skipUnless(CONTAINER_IMAGE, "EVOAGENT_TEST_CONTAINER_IMAGE is not configured")
class ContainerVerifierIntegrationTests(unittest.TestCase):
    def test_real_container_launch_without_acknowledgement_is_cleaned_up(self):
        actual_run = subprocess.run
        names = []

        def lose_acknowledgement(command, **kwargs):
            if command[:2] != ["docker", "run"]:
                return actual_run(command, **kwargs)
            name = command[command.index("--name") + 1]
            names.append(name)
            self.addCleanup(
                actual_run,
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            result = actual_run(command, **kwargs)
            self.assertEqual(0, result.returncode, "the real container must start first")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        verifier = RepairVerifier(
            test_command="python -c 'print(1)'",
            timeout_seconds=10,
            container_image=CONTAINER_IMAGE,
            memory_mb=128,
            pids_limit=16,
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch("evoagent.verifier.subprocess.run", side_effect=lose_acknowledgement),
            patch.object(verifier, "_execute") as execute,
        ):
            result = verifier.verify_worktree(root)
        self.assertEqual("timeout", result["status"])
        self.assertFalse(result["passed"])
        execute.assert_not_called()
        self.assertEqual(1, len(names))
        containers = actual_run(
            ["docker", "ps", "-a", "--filter", "name=" + names[0], "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual("", containers.stdout.strip())

    def test_real_container_executes_verified_skill_snapshot(self):
        source = """\
import os

from evoagent.reviewer import Reviewer

class EmptyReviewer(Reviewer):
    name = "container-snapshot"

    def review(self, diff, parsed):
        assert os.geteuid() == 65534
        assert os.getegid() == 65534
        return []

def create_skill():
    return EmptyReviewer()
"""
        diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+value = 1\n"
        reviewer = SandboxedSkillReviewer(
            "container-snapshot",
            "/skill/skill.py",
            container_image=CONTAINER_IMAGE,
            source=source,
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        )

        self.assertEqual([], reviewer.review(diff, parse_unified_diff(diff)))

    def test_real_container_skill_output_is_bounded_and_cleaned_up(self):
        source = """\
from evoagent.reviewer import Reviewer

class NoisyReviewer(Reviewer):
    name = "noisy"

    def review(self, diff, parsed):
        while True:
            print("x" * 65536)

def create_skill():
    return NoisyReviewer()
"""
        reviewer = SandboxedSkillReviewer(
            "noisy",
            "/skill/skill.py",
            timeout_seconds=5,
            container_image=CONTAINER_IMAGE,
            source=source,
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        )

        with self.assertRaisesRegex(RuntimeError, "output limit"):
            reviewer.review("", parse_unified_diff(""))

        containers = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=evoagent-skill-",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual("", containers.stdout.strip())

    def test_real_container_has_expected_security_boundary(self):
        with tempfile.TemporaryDirectory(prefix="evoagent-source,private:") as root:
            probe = os.path.join(root, "probe.py")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write(
                    """import os
import pathlib
import socket

assert os.geteuid() == 65534
assert os.getegid() == 65534
probe = pathlib.Path('/work/probe.py')
assert probe.is_file()
assert probe.stat().st_uid == os.geteuid()
assert probe.stat().st_mode & 0o777 == 0o600
mounts = pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
assert any(line.split()[4] == '/work' and ' - tmpfs ' in line for line in mounts)
assert all(line.split()[4] != '/source' for line in mounts)
probe.write_text('# changed only in container')
assert 'EVOAGENT_CI_HOST_SECRET' not in os.environ
pathlib.Path('/tmp/evoagent-probe').write_text('ok')
try:
    pathlib.Path('/evoagent-root-write').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('container root filesystem is writable')
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('container has external network access')
"""
                )
            os.chmod(probe, 0o600)
            with open(probe, encoding="utf-8") as handle:
                original = handle.read()
            os.environ["EVOAGENT_CI_HOST_SECRET"] = "must-not-cross-boundary"
            self.addCleanup(os.environ.pop, "EVOAGENT_CI_HOST_SECRET", None)
            verifier = RepairVerifier(
                test_command="python probe.py",
                timeout_seconds=10,
                container_image=CONTAINER_IMAGE,
                memory_mb=256,
                pids_limit=32,
                cpus=0.5,
            )
            result = verifier.verify_worktree(root)
            with open(probe, encoding="utf-8") as handle:
                self.assertEqual(original, handle.read())
        self.assertTrue(result["passed"], result)

    def test_real_container_timeout_is_bounded_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as root:
            verifier = RepairVerifier(
                test_command="python -c 'import time; time.sleep(30)'",
                timeout_seconds=1,
                container_image=CONTAINER_IMAGE,
                memory_mb=128,
                pids_limit=16,
            )
            started = time.monotonic()
            result = verifier.verify_worktree(root)
            elapsed = time.monotonic() - started
        self.assertEqual("timeout", result["status"])
        self.assertLess(elapsed, 10)
        containers = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "name=evoagent-verify-",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual("", containers.stdout.strip())


if __name__ == "__main__":
    unittest.main()
