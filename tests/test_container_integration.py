"""Real Docker execution checks, enabled only in the container CI job."""

import hashlib
import os
import subprocess
import tempfile
import time
import unittest

from evoagent.diff_parser import parse_unified_diff
from evoagent.skills import SandboxedSkillReviewer
from evoagent.verifier import RepairVerifier

CONTAINER_IMAGE = os.getenv("EVOAGENT_TEST_CONTAINER_IMAGE", "")


@unittest.skipUnless(CONTAINER_IMAGE, "EVOAGENT_TEST_CONTAINER_IMAGE is not configured")
class ContainerVerifierIntegrationTests(unittest.TestCase):
    def test_real_container_executes_verified_skill_snapshot(self):
        source = """\
import os

from evoagent.reviewer import Reviewer

class EmptyReviewer(Reviewer):
    name = "container-snapshot"

    def review(self, diff, parsed):
        assert os.geteuid() != 0
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
        with tempfile.TemporaryDirectory() as root:
            probe = os.path.join(root, "probe.py")
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write(
                    """import os
import pathlib
import socket

assert pathlib.Path('/work/probe.py').is_file()
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
