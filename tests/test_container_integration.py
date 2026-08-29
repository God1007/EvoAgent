"""Opt-in real Docker execution checks for CI and local isolated runtimes."""

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from evoagent.container_runtime import sandbox_command
from evoagent.diff_parser import parse_unified_diff
from evoagent.proof import LocalProofExecutor, ProofRunner
from evoagent.proof_service import ProofServer, SocketProofExecutor
from evoagent.skills import SandboxedSkillReviewer, resolve_container_image
from evoagent.verifier import RepairVerifier

CONTAINER_IMAGE = os.getenv("EVOAGENT_TEST_CONTAINER_IMAGE", "")


@unittest.skipUnless(CONTAINER_IMAGE, "EVOAGENT_TEST_CONTAINER_IMAGE is not configured")
class ContainerVerifierIntegrationTests(unittest.TestCase):
    def test_expired_paused_sandbox_is_reconciled_before_new_work(self):
        name = "evoagent-verify-%s" % uuid.uuid4().hex[:12]
        with patch("evoagent.container_runtime.time.time", return_value=0):
            command = sandbox_command(
                CONTAINER_IMAGE,
                name,
                1,
                ["--pids-limit", "16", "--memory", "128m", "--cpus", "0.25"],
            )
        self.addCleanup(
            subprocess.run,
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )
        subprocess.run(
            ["docker", "pause", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )

        with tempfile.TemporaryDirectory() as root:
            result = RepairVerifier(
                test_command="python -c 'print(1)'",
                timeout_seconds=10,
                container_image=CONTAINER_IMAGE,
                memory_mb=128,
                pids_limit=16,
            ).verify_worktree(root)

        self.assertTrue(result["passed"], result)
        remaining = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=" + name, "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual("", remaining.stdout.strip())

    def test_real_sandboxes_expire_after_owner_is_killed(self):
        for kind in ("verify", "skill"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                nonce = uuid.uuid4().hex
                name = "evoagent-%s-%s" % (kind, nonce[:12])
                script = """import hashlib, sys, uuid
from unittest.mock import patch
from evoagent.diff_parser import parse_unified_diff
from evoagent.skills import SandboxedSkillReviewer
from evoagent.verifier import RepairVerifier

kind, image, nonce, root = sys.argv[1:]
with patch('uuid.uuid4', return_value=uuid.UUID(hex=nonce)):
    if kind == 'verify':
        RepairVerifier("python -c 'import time; time.sleep(60)'", 5,
                       container_image=image, require_container=True).verify_worktree(root)
    else:
        source = '''import time
from evoagent.reviewer import Reviewer
class Slow(Reviewer):
    name = "slow"
    def review(self, diff, parsed):
        time.sleep(60)
        return []
def create_skill(): return Slow()
'''
        SandboxedSkillReviewer('slow', '/skill/skill.py', timeout_seconds=5,
            container_image=image, source=source,
            source_sha256=hashlib.sha256(source.encode()).hexdigest()).review('', parse_unified_diff(''))
"""

                def containers(name=name):
                    return subprocess.check_output(
                        [
                            "docker",
                            "ps",
                            "-a",
                            "--filter",
                            "name=" + name,
                            "--format",
                            "{{.Names}}",
                        ],
                        text=True,
                        timeout=3,
                    ).strip()

                owner = subprocess.Popen(
                    [sys.executable, "-c", script, kind, CONTAINER_IMAGE, nonce, root],
                    env={**os.environ, "TMPDIR": root},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not containers() and time.monotonic() < deadline:
                        self.assertIsNone(owner.poll(), "owner must reach real Docker execution")
                        time.sleep(0.05)
                    self.assertEqual(name, containers())
                    self.assertIsNone(owner.poll())
                    owner.kill()
                    owner.wait(timeout=3)
                    self.assertLess(owner.returncode, 0, "real forced owner termination")
                    deadline = time.monotonic() + 23
                    while containers() and time.monotonic() < deadline:
                        time.sleep(0.25)
                    self.assertEqual(
                        "", containers(), "lost owners must not leave immortal sandboxes"
                    )
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.wait(timeout=3)
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                    )

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
        assert 'EVOAGENT_CI_HOST_SECRET' not in os.environ
        assert not any(key.startswith('DOCKER_') for key in os.environ)
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'ALL_PROXY', 'NO_PROXY'):
            assert not os.environ.get(key)
            assert not os.environ.get(key.lower())
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

        with patch.dict(os.environ, {"EVOAGENT_CI_HOST_SECRET": "must-not-cross-boundary"}):
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
                    """import ctypes
import errno
import os
import pathlib
import signal
import socket

assert os.geteuid() == 65534
assert os.getegid() == 65534
assert pathlib.Path('/proc/1/comm').read_text().strip() == 'sleep'
status = pathlib.Path('/proc/1/status').read_text().splitlines()
assert next(line for line in status if line.startswith('Uid:')).split()[1:] == ['65533'] * 4
# The deadline has a separate non-root identity, not a reliance on Yama defaults.
for sig in (signal.SIGSTOP, signal.SIGKILL):
    try:
        os.kill(1, sig)
    except PermissionError:
        pass
    else:
        raise AssertionError('untrusted code can signal the deadline')
try:
    with open('/proc/1/mem', 'r+b', buffering=0):
        raise AssertionError('untrusted code can open deadline memory')
except PermissionError:
    pass
libc = ctypes.CDLL(None, use_errno=True)
assert libc.ptrace(16, 1, None, None) == -1
assert ctypes.get_errno() == errno.EPERM
probe = pathlib.Path('/work/probe.py')
assert probe.is_file()
assert probe.stat().st_uid == os.geteuid()
assert probe.stat().st_mode & 0o777 == 0o600
mounts = pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
assert any(line.split()[4] == '/work' and ' - tmpfs ' in line for line in mounts)
assert all(line.split()[4] != '/source' for line in mounts)
probe.write_text('# changed only in container')
assert 'EVOAGENT_CI_HOST_SECRET' not in os.environ
assert not any(key.startswith('DOCKER_') for key in os.environ)
for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'FTP_PROXY', 'ALL_PROXY', 'NO_PROXY'):
    assert not os.environ.get(key)
    assert not os.environ.get(key.lower())
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

    def test_real_container_proof_reaches_l4_with_matching_image_attestations(self):
        image = resolve_container_image(CONTAINER_IMAGE)
        runner = ProofRunner(
            lambda command: RepairVerifier(
                test_command=command,
                timeout_seconds=10,
                container_image=image,
                require_container=True,
                memory_mb=128,
                pids_limit=16,
            )
        )
        result = runner.prove(
            {"app.py": "def clamp(value): return min(value, 10)\n"},
            {"app.py": "def clamp(value): return max(0, min(value, 10))\n"},
            "python -c 'from app import clamp; assert clamp(-1) == 0'",
            "python -c 'from app import clamp; assert [clamp(v) for v in (0, 5, 20)] == [0, 5, 10]'",
        )
        self.assertEqual("L4-regression-clean", result["evidence_label"], result)
        self.assertEqual(["failed", "passed", "passed"], [s["status"] for s in result["steps"]])
        for step in result["steps"]:
            self.assertEqual("container", step["attestation"]["execution_mode"])
            self.assertEqual(image, step["attestation"]["container_image"])

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

    def test_real_socket_executor_proves_l4_with_request_bound_attestations(self):
        image = resolve_container_image(CONTAINER_IMAGE)
        executor = LocalProofExecutor(
            lambda command: RepairVerifier(
                command, 10, container_image=image, require_container=True
            )
        )
        with tempfile.TemporaryDirectory() as root:
            with ProofServer(os.path.join(root, "proof.sock"), executor, image, 2) as server:
                worker = threading.Thread(target=server.serve_forever)
                worker.start()
                try:
                    client = SocketProofExecutor(server.path, 10)
                    original = {"app.py": "def clamp(value): return min(value, 10)\n"}
                    patched = {"app.py": "def clamp(value): return max(0, min(value, 10))\n"}
                    result = ProofRunner(executor=client).prove(
                        original,
                        patched,
                        "python -c 'from app import clamp; assert clamp(-1) == 0'",
                        "python -c 'from app import clamp; assert clamp(5) == 5'",
                    )
                finally:
                    server.shutdown()
                    worker.join(5)
        self.assertEqual(4, result["evidence_level"], result)
        self.assertEqual(["failed", "passed", "passed"], [s["status"] for s in result["steps"]])
        requests = set()
        for step in result["steps"]:
            attestation = step["attestation"]
            self.assertEqual(image, attestation["container_image"])
            self.assertEqual("container", attestation["execution_mode"])
            self.assertRegex(attestation["request_sha256"], "^[0-9a-f]{64}$")
            requests.add(attestation["request_sha256"])
        self.assertEqual(3, len(requests), "each file/command combination has separate evidence")


if __name__ == "__main__":
    unittest.main()
