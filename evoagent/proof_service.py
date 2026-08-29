"""Private, bounded Proof execution over a filesystem-protected Unix socket.

The API gets this socket, never the Docker socket. Only the operator selects the
image and resource limits; requests can supply text files and a test command.
"""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import socket
import socketserver
import stat
import threading
import time
from typing import Any

from .backpressure import ConcurrencyLimiter
from .errors import safe_exception_fields
from .json_boundary import strict_json_loads
from .proof import MAX_PROOF_FILES, LocalProofExecutor, _proof_target
from .skills import resolve_container_image
from .verifier import RepairVerifier

PROTOCOL_VERSION = 1
# ponytail: bounded text-only jobs; use artifact storage when real jobs exceed 16 MiB.
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_COMMAND_BYTES = 8192
IO_TIMEOUT = 5.0


def _encode(value: object, limit: int) -> bytes:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(body) > limit:
        raise ValueError("proof protocol message is too large")
    return body


def _receive(connection: socket.socket, limit: int, deadline: float) -> bytes:
    def read_exact(size: int) -> bytes:
        value = bytearray()
        while len(value) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("proof protocol deadline exceeded")
            connection.settimeout(remaining)
            chunk = connection.recv(min(size - len(value), 65536))
            if not chunk:
                raise ValueError("incomplete proof protocol message")
            value.extend(chunk)
        return bytes(value)

    length = int.from_bytes(read_exact(4), "big")
    if not 0 < length <= limit:
        raise ValueError("invalid proof protocol message length")
    return read_exact(length)


def _send(connection: socket.socket, body: bytes, timeout: float) -> None:
    if timeout <= 0:
        raise TimeoutError("proof protocol deadline exceeded")
    connection.settimeout(timeout)
    connection.sendall(len(body).to_bytes(4, "big") + body)


def _validate_job(value: dict) -> None:
    if set(value) != {"version", "operation", "files", "command"}:
        raise ValueError("invalid proof job fields")
    files, command = value["files"], value["command"]
    if not isinstance(files, dict) or len(files) > MAX_PROOF_FILES:
        raise ValueError("invalid proof file set")
    for path, content in files.items():
        if not isinstance(content, str):
            raise ValueError("proof files must contain text")
        _proof_target(os.curdir, path)
    if (
        not isinstance(command, str)
        or not command.strip()
        or "\0" in command
        or len(command.encode("utf-8")) > MAX_COMMAND_BYTES
    ):
        raise ValueError("invalid proof command")


class SocketProofExecutor:
    """The existing ProofExecutorPort, without granting Docker access to the API."""

    def __init__(self, path: str, timeout_seconds: int = 120):
        if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
            raise ValueError("proof executor socket must be an absolute path")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("proof executor timeout must be a positive integer")
        self.path = path
        self.timeout_seconds = timeout_seconds
        # Pin the service's immutable runtime image before admitting API traffic.
        health = self._exchange({"version": PROTOCOL_VERSION, "operation": "health"}, IO_TIMEOUT)
        image = health.get("container_image")
        if (
            set(health) != {"container_image"}
            or not isinstance(image, str)
            or not image.startswith("sha256:")
            or len(image) != 71
            or any(character not in "0123456789abcdef" for character in image[7:])
        ):
            raise ValueError("invalid proof executor image")
        self.container_image = image

    def _exchange(self, value: dict, timeout: float) -> dict[str, Any]:
        body = _encode(value, MAX_REQUEST_BYTES)
        digest = hashlib.sha256(body).hexdigest()
        deadline = time.monotonic() + timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(self.path)
            _send(connection, body, deadline - time.monotonic())
            response = strict_json_loads(_receive(connection, MAX_RESPONSE_BYTES, deadline))
        if (
            not isinstance(response, dict)
            or set(response) != {"version", "request_sha256", "result"}
            or type(response["version"]) is not int
            or response["version"] != PROTOCOL_VERSION
            or response["request_sha256"] != digest
            or not isinstance(response["result"], dict)
        ):
            raise ValueError("invalid proof executor response")
        return response["result"]

    def execute(self, files: dict[str, str], command: str) -> dict[str, Any]:
        request = {
            "version": PROTOCOL_VERSION,
            "operation": "execute",
            "files": files,
            "command": command,
        }
        _validate_job(request)
        result = self._exchange(request, self.timeout_seconds + 20)
        attestation = result.get("attestation")
        if not isinstance(attestation, dict) or any(
            attestation.get(key) != expected
            for key, expected in {
                "execution_mode": "container",
                "container_image": self.container_image,
                "test_command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            }.items()
        ):
            raise ValueError("proof executor attestation mismatch")
        # Bind the retained evidence to the exact files AND command sent to the service.
        result["attestation"] = {
            **attestation,
            "request_sha256": hashlib.sha256(_encode(request, MAX_REQUEST_BYTES)).hexdigest(),
        }
        return result

    def healthy(self) -> bool:
        try:
            return self._exchange(
                {"version": PROTOCOL_VERSION, "operation": "health"}, min(IO_TIMEOUT, 0.5)
            ) == {"container_image": self.container_image}
        except Exception:
            return False


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        try:
            body = _receive(self.request, MAX_REQUEST_BYTES, time.monotonic() + IO_TIMEOUT)
            value = strict_json_loads(body)
            if (
                not isinstance(value, dict)
                or type(value.get("version")) is not int
                or value["version"] != PROTOCOL_VERSION
            ):
                raise ValueError("unsupported proof protocol")
            server: ProofServer = self.server  # type: ignore[assignment]
            if value.get("operation") == "health" and set(value) == {"version", "operation"}:
                result = {"container_image": server.container_image}
            elif value.get("operation") == "execute":
                _validate_job(value)
                if not server.jobs.try_acquire():
                    raise RuntimeError("proof executor at capacity")
                try:
                    result = server.executor.execute(value["files"], value["command"])
                finally:
                    server.jobs.release()
            else:
                raise ValueError("unsupported proof operation")
            response = {
                "version": PROTOCOL_VERSION,
                "request_sha256": hashlib.sha256(body).hexdigest(),
                "result": result,
            }
            _send(self.request, _encode(response, MAX_RESPONSE_BYTES), IO_TIMEOUT)
        except Exception as exc:
            # Close on invalid input, overload or infrastructure failure. Never send a fake
            # test failure or copy request contents/exception messages to the peer or logs.
            try:
                print(
                    json.dumps({"event": "proof_service_rejected", **safe_exception_fields(exc)}),
                    flush=True,
                )
            except (OSError, ValueError):
                pass


class ProofServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    request_queue_size = 8

    def __init__(self, path: str, executor: LocalProofExecutor, container_image: str, workers: int):
        if not os.path.isabs(path) or type(workers) is not int or not 1 <= workers <= 16:
            raise ValueError("proof server requires an absolute socket path and 1-16 workers")
        self.executor = executor
        self.container_image = container_image
        self.jobs = ConcurrencyLimiter(workers)
        # Leave a bounded connection slot for health/rejections during full execution.
        self.gate = ConcurrencyLimiter(workers + 1)
        self.path = path
        self._socket_identity = None
        self._lock_file = open(path + ".lock", "a", encoding="utf-8")
        try:
            # One owner may recover an owned stale socket; never unlink a live peer's socket.
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.lexists(path):
                previous = os.lstat(path)
                if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid():
                    raise ValueError("refusing to replace a non-owned proof socket")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(IO_TIMEOUT)
                    try:
                        probe.connect(path)
                    except ConnectionRefusedError:
                        pass
                    else:
                        raise ValueError("proof socket is already listening")
                os.unlink(path)
            super().__init__(path, _Handler)
            bound = os.lstat(path)
            self._socket_identity = (bound.st_dev, bound.st_ino)
            os.chmod(path, 0o600)
        except BaseException:
            if hasattr(self, "socket"):
                self.server_close()
            self._lock_file.close()
            raise

    def process_request(self, request, client_address) -> None:
        if not self.gate.try_acquire():
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.gate.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.gate.release()

    def server_close(self) -> None:
        try:
            super().server_close()  # drain bounded in-flight work before releasing the socket
            if self._socket_identity and os.path.lexists(self.path):
                bound = os.lstat(self.path)
                if (bound.st_dev, bound.st_ino) == self._socket_identity:
                    os.unlink(self.path)
        finally:
            self._lock_file.close()


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--memory-mb", type=int, default=256)
    parser.add_argument("--pids-limit", type=int, default=64)
    parser.add_argument("--cpus", type=float, default=0.5)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args(argv)
    image = resolve_container_image(args.image)
    if not image:
        parser.error("--image must name a preloaded container image")

    def verifier(command):
        return RepairVerifier(
            command,
            args.timeout,
            container_image=image,
            require_container=True,
            memory_mb=args.memory_mb,
            pids_limit=args.pids_limit,
            cpus=args.cpus,
        )

    verifier("true")  # validate all operator resource limits before opening the socket
    with ProofServer(args.socket, LocalProofExecutor(verifier), image, args.workers) as server:
        stopping = threading.Event()

        def stop(_signal, _frame):
            if not stopping.is_set():
                stopping.set()
                threading.Thread(target=server.shutdown, daemon=True).start()

        for name in (signal.SIGINT, signal.SIGTERM):
            signal.signal(name, stop)
        server.serve_forever(poll_interval=0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
