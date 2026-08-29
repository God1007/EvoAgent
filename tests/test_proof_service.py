"""Real Unix-socket contracts; container execution is covered separately."""

import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from evoagent.bootstrap import _proof_executor
from evoagent.proof import ProofRunner
from evoagent.proof_service import (
    MAX_REQUEST_BYTES,
    ProofServer,
    SocketProofExecutor,
    _encode,
    _receive,
    _send,
)
from tests.test_http_server import _settings

IMAGE = "sha256:" + "a" * 64


def outcome(_files, command):
    return {
        "passed": True,
        "status": "passed",
        "checks": [{"detail": "verified"}],
        "duration_seconds": 0.01,
        "attestation": {
            "execution_mode": "container",
            "container_image": IMAGE,
            "test_command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        },
    }


class ProofServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "proof.sock")
        self.executor = mock.Mock()
        self.executor.execute.side_effect = outcome

    def start(self, workers=2):
        server = ProofServer(self.path, self.executor, IMAGE, workers)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        return server

    def raw(self, body):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(self.path)
            _send(peer, body, 2)
            return _receive(peer, 256 * 1024, time.monotonic() + 2)

    def test_exact_payload_attestation_and_bootstrap_without_docker_in_api(self):
        self.start()
        settings = _settings(proof_executor_socket=self.path)
        with mock.patch("evoagent.skills.subprocess.run", side_effect=AssertionError("Docker")):
            client = _proof_executor(settings, "")
            result = client.execute({"目录/app.py": "print('hello')\n"}, "python 目录/app.py")
        self.executor.execute.assert_called_once_with(
            {"目录/app.py": "print('hello')\n"}, "python 目录/app.py"
        )
        self.assertEqual(IMAGE, result["attestation"]["container_image"])
        self.assertEqual(64, len(result["attestation"]["request_sha256"]))
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)

    def test_health_reflects_the_live_pinned_executor(self):
        self.start()
        client = SocketProofExecutor(self.path)
        self.assertTrue(client.healthy())
        for response in ({"container_image": "sha256:" + "b" * 64}, OSError("offline")):
            with (
                self.subTest(response=type(response).__name__),
                mock.patch.object(
                    client,
                    "_exchange",
                    side_effect=response if isinstance(response, Exception) else None,
                    return_value=response if isinstance(response, dict) else None,
                ),
            ):
                self.assertFalse(client.healthy())

    def test_invalid_jobs_and_oversized_frames_never_reach_execution(self):
        self.start()
        base = {"version": 1, "operation": "execute", "files": {}, "command": "true"}
        requests = [
            {**base, "version": True},
            {**base, "version": 2},
            {**base, "operation": "run-docker"},
            {**base, "image": "other"},
            {**base, "env": {"SECRET": "private"}},
            {**base, "mounts": ["/"]},
            {**base, "files": {"../escape": "private"}},
            {**base, "files": {"/escape": "private"}},
            {**base, "files": {"app.py": "first", "./app.py": "second"}},
            {**base, "files": {"app.py": 1}},
            {**base, "files": []},
            {**base, "command": ""},
            {**base, "command": "\0"},
            {**base, "command": "x" * 8193},
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises((ValueError, ConnectionError)):
                self.raw(_encode(request, MAX_REQUEST_BYTES))
        with self.assertRaises((ValueError, ConnectionError)):
            self.raw(b'{"version":1,"version":1,"operation":"health"}')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(self.path)
            peer.sendall((MAX_REQUEST_BYTES + 1).to_bytes(4, "big"))
            self.assertEqual(b"", peer.recv(1))
        self.executor.execute.assert_not_called()

    def test_tampered_result_is_inconclusive_not_a_reproduced_bug(self):
        self.start()
        client = SocketProofExecutor(self.path)
        for field, value in (
            ("execution_mode", "host"),
            ("container_image", "sha256:" + "b" * 64),
            ("test_command_sha256", "other"),
        ):
            result = outcome({}, "test")
            result.update(passed=False, status="failed")
            result["attestation"][field] = value
            self.executor.execute.side_effect = None
            self.executor.execute.return_value = result
            with self.subTest(field=field):
                proof = ProofRunner(executor=client).prove({}, {}, "test")
                self.assertEqual(1, proof["evidence_level"])
                self.assertEqual("error", proof["steps"][0]["status"])

        def tamper(connection, body, timeout):
            value = json.loads(body)
            if "request_sha256" in value:
                value["request_sha256"] = "other"
                body = json.dumps(value).encode()
            return _send(connection, body, timeout)

        with (
            mock.patch("evoagent.proof_service._send", side_effect=tamper),
            self.assertRaisesRegex(ValueError, "invalid proof executor response"),
        ):
            client._exchange({"version": 1, "operation": "health"}, 2)

    def test_malformed_result_transport_loss_and_oversize_fail_closed(self):
        self.start()
        client = SocketProofExecutor(self.path)
        for result in (None, [], {**outcome({}, "test"), "passed": "yes"}):
            self.executor.execute.side_effect = None
            self.executor.execute.return_value = result
            proof = ProofRunner(executor=client).prove({}, {}, "test")
            self.assertEqual(1, proof["evidence_level"])
            self.assertEqual("error", proof["steps"][0]["status"])
        self.executor.execute.side_effect = RuntimeError("private token")
        proof = ProofRunner(executor=client).prove({}, {}, "test")
        self.assertEqual("error", proof["steps"][0]["status"])
        self.assertNotIn("private token", str(proof))
        self.executor.reset_mock()
        with mock.patch("evoagent.proof_service.MAX_REQUEST_BYTES", 128):
            with self.assertRaises(ValueError):
                client.execute({"app.py": "x" * 256}, "test")
        self.executor.execute.assert_not_called()

    def test_concurrency_is_bounded_and_partial_requests_have_a_deadline(self):
        server = self.start(workers=1)
        client = SocketProofExecutor(self.path)
        entered, release = threading.Event(), threading.Event()

        def slow(files, command):
            entered.set()
            release.wait(2)
            return outcome(files, command)

        self.executor.execute.side_effect = slow
        job = threading.Thread(target=client.execute, args=({}, "test"))
        job.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(1, server.jobs.in_flight())
            # Running at capacity must not make a healthy executor fail its probe.
            self.assertEqual(IMAGE, SocketProofExecutor(self.path).container_image)
            with self.assertRaises((ValueError, ConnectionError)):
                client.execute({}, "second")
            self.executor.execute.assert_called_once()
        finally:
            release.set()
            job.join(2)
        with mock.patch("evoagent.proof_service.IO_TIMEOUT", 0.05):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(2)
                peer.connect(self.path)
                peer.sendall(b"\0")
                self.assertEqual(b"", peer.recv(1))

    def test_stale_socket_recovers_but_active_or_regular_paths_are_preserved(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(self.path)
        self.start()
        inode = os.stat(self.path).st_ino
        with self.assertRaises(BlockingIOError):
            ProofServer(self.path, self.executor, IMAGE, 1)
        self.assertEqual(inode, os.stat(self.path).st_ino)
        self.assertEqual(IMAGE, SocketProofExecutor(self.path).container_image)
        regular = str(Path(self.temp.name) / "keep.txt")
        Path(regular).write_text("keep")
        with self.assertRaises(ValueError):
            ProofServer(regular, self.executor, IMAGE, 1)
        self.assertEqual("keep", Path(regular).read_text())
        live_path = str(Path(self.temp.name) / "other.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(live_path)
            live.listen()
            inode = os.stat(live_path).st_ino
            with self.assertRaises(ValueError):
                ProofServer(live_path, self.executor, IMAGE, 1)
            self.assertEqual(inode, os.stat(live_path).st_ino)

    def test_missing_service_or_invalid_configuration_never_falls_back_to_host(self):
        with self.assertRaises(OSError):
            _proof_executor(_settings(proof_executor_socket=self.path), "")
        for path, timeout in (("relative.sock", 1), (self.path, 0), (self.path, True)):
            with self.subTest(path=path, timeout=timeout), self.assertRaises(ValueError):
                SocketProofExecutor(path, timeout)


if __name__ == "__main__":
    unittest.main()
