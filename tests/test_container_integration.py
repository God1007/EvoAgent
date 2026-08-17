"""Real Docker execution checks, enabled only in the container CI job."""

import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from evoagent.proof import LocalProofExecutor
from evoagent.proof_remote import (
    ContentAddressedArtifactStore,
    ProofRunnerServer,
    RemoteProofExecutor,
    _handler,
)
from evoagent.verifier import RepairVerifier

CONTAINER_IMAGE = os.getenv("EVOAGENT_TEST_CONTAINER_IMAGE", "")


@unittest.skipUnless(CONTAINER_IMAGE, "EVOAGENT_TEST_CONTAINER_IMAGE is not configured")
class ContainerVerifierIntegrationTests(unittest.TestCase):
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

    def test_remote_runner_executes_in_container_and_attests_artifacts(self):
        secret = "integration-proof-signing-key-at-least-32-bytes"
        source = """import os
import socket

assert 'EVOAGENT_CI_HOST_SECRET' not in os.environ
try:
    socket.create_connection(('1.1.1.1', 80), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('proof job has external network access')
"""
        os.environ["EVOAGENT_CI_HOST_SECRET"] = "must-not-cross-boundary"
        self.addCleanup(os.environ.pop, "EVOAGENT_CI_HOST_SECRET", None)
        with tempfile.TemporaryDirectory() as artifacts:
            executor = LocalProofExecutor(
                lambda command: RepairVerifier(
                    command,
                    timeout_seconds=10,
                    container_image=CONTAINER_IMAGE,
                    memory_mb=256,
                    pids_limit=32,
                    cpus=0.5,
                    require_container=True,
                )
            )
            service = ProofRunnerServer(
                executor,
                secret,
                artifact_store=ContentAddressedArtifactStore(artifacts),
                require_artifacts=True,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(service, 1024 * 1024))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = RemoteProofExecutor(
                    "http://127.0.0.1:%d" % server.server_address[1],
                    secret,
                    ("127.0.0.1",),
                )
                result = client.execute({"probe.py": source}, "python probe.py")
            finally:
                server.shutdown()
                server.server_close()

        self.assertTrue(result["passed"], result)
        self.assertTrue(result["attestation"]["artifacts"]["input"].startswith("sha256:"))
        self.assertTrue(result["attestation"]["artifacts"]["evidence"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
