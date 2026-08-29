"""Compilation and test gates for generated repairs.

Running a repository's own test command means executing code that originated
from an untrusted pull request. Two isolation modes are provided:

* Container mode (recommended for untrusted code): when a container image is
  configured the command runs in ``docker`` with networking disabled, a
  read-only root filesystem, a non-root user, dropped capabilities, and
  CPU/memory/PID/file-size limits.
* Host fallback mode: when no image is configured the command runs on the host
  under best-effort shell ``ulimit`` bounds (CPU time, file size, process
  count) in its own session so timeouts kill the whole process group. Host mode
  is **not** network-isolated and ``RLIMIT_NPROC`` is per-UID, so it must only
  be used by trusted direct library callers. Application repair and Proof paths
  refuse host mode.

Output is read with an upper bound so an untrusted test that floods stdout
cannot exhaust host memory, and archive extraction is bounded against zip-slip
and compression bombs.
"""

import hashlib
import math
import os
import selectors
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from io import BytesIO

from .container_runtime import reconcile_sandboxes, remove_sandbox, sandbox_command
from .errors import safe_exception_summary
from .metrics import metrics


class RepairVerifier:
    MAX_ARCHIVE_MEMBERS = 100_000
    MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024

    def __init__(
        self,
        test_command: str = "",
        timeout_seconds: int = 120,
        container_image: str = "",
        memory_mb: int = 1024,
        pids_limit: int = 256,
        max_output_bytes: int = 16000,
        max_file_mb: int = 256,
        cpus: float = 1.0,
        require_container: bool = False,
    ):
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (
                timeout_seconds,
                memory_mb,
                pids_limit,
                max_output_bytes,
                max_file_mb,
            )
        ):
            raise ValueError("repair verifier resource limits must be positive integers")
        if (
            isinstance(cpus, bool)
            or not isinstance(cpus, (int, float))
            or not math.isfinite(cpus)
            or cpus <= 0
        ):
            raise ValueError("repair verifier CPU limit must be positive and finite")
        self.test_command = test_command
        self.timeout_seconds = timeout_seconds
        self.container_image = container_image
        self.memory_mb = memory_mb
        self.pids_limit = pids_limit
        self.max_output_bytes = max_output_bytes
        self.max_file_mb = max_file_mb
        self.cpus = cpus
        self.require_container = require_container
        self.max_archive_uncompressed_bytes = min(
            self.MAX_ARCHIVE_UNCOMPRESSED_BYTES, memory_mb * 1024 * 1024
        )

    def verify_contents(self, files: dict[str, str]) -> dict:
        started = time.monotonic()
        checks = []
        for path, content in files.items():
            if path.endswith(".py"):
                try:
                    compile(content, path, "exec")
                    checks.append({"name": "compile:%s" % path, "passed": True})
                except SyntaxError as exc:
                    checks.append(
                        {
                            "name": "compile:%s" % path,
                            "passed": False,
                            "detail": "%s:%s: %s" % (path, exc.lineno, exc.msg),
                        }
                    )
        return {
            "passed": all(item["passed"] for item in checks),
            "checks": checks,
            "duration_seconds": round(time.monotonic() - started, 4),
        }

    def verify_worktree(self, root: str) -> dict:
        if not self.test_command:
            return {
                "passed": True,
                "status": "skipped",
                "checks": [],
                "note": "No repository test command configured.",
                "duration_seconds": 0.0,
            }
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            raise ValueError("verification worktree does not exist")
        started = time.monotonic()
        if self.container_image:
            execution_mode = "container"
            status, detail = self._run_in_container(root)
        elif self.require_container:
            execution_mode = "none"
            status, detail = (
                "error",
                "container isolation is required but no repair container image is configured.",
            )
        else:
            execution_mode = "host"
            status, detail = self._run_on_host(root)
        passed = status == "passed"
        attestation = {
            "execution_mode": execution_mode,
            "test_command_sha256": hashlib.sha256(self.test_command.encode()).hexdigest(),
        }
        if self.container_image:
            attestation["container_image"] = self.container_image
        # ``status`` distinguishes a genuine non-zero test result ("failed") from
        # non-verdict outcomes ("timeout"/"error") so callers such as the Proof
        # Runner never misread an infra failure as a reproduced bug.
        return {
            "passed": passed,
            "status": status,
            "checks": [
                {"name": "repository-tests", "passed": passed, "status": status, "detail": detail}
            ],
            "attestation": attestation,
            "duration_seconds": round(time.monotonic() - started, 4),
        }

    def _execute(
        self,
        command: list[str],
        cwd: str | None,
        env: dict | None,
        timeout_seconds: float | None = None,
    ) -> tuple:
        """Run a command, bounding output size and killing the process group on timeout."""
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            return None, "", True
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
        except (FileNotFoundError, OSError) as exc:
            return None, safe_exception_summary(exc, "verification launch failed"), False
        if process.stdout is None:  # pragma: no cover - defensive
            return process.wait(), "", False
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        timed_out = False
        if os.name == "nt":  # pragma: no cover - Windows host mode is not memory-bounded
            try:
                out, _ = process.communicate(timeout=timeout)
                buffer.extend(out or b"")
            except subprocess.TimeoutExpired:
                self._terminate(process)
                process.communicate()
                timed_out = True
        else:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            # Drain to EOF (or the deadline) so the process is never killed merely for
            # being verbose; keep only a rolling tail so memory stays bounded and the
            # retained detail is the END of output (where failures are reported).
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(timeout=remaining):
                    timed_out = True
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > self.max_output_bytes:
                    del buffer[: -self.max_output_bytes]
            selector.close()
            if timed_out:
                # Unconditional: kills a quiet grandchild that outlived the leader.
                self._terminate(process)
        # Preserve the real exit code, bounding a clean-EOF wait by the remaining budget.
        grace = 5.0 if timed_out else max(1.0, deadline - time.monotonic())
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            timed_out = True
        try:
            process.stdout.close()
        except OSError:  # pragma: no cover - defensive
            pass
        detail = bytes(buffer[-self.max_output_bytes :]).decode("utf-8", errors="replace")
        return process.returncode, detail, timed_out

    def _terminate(self, process: subprocess.Popen) -> None:
        if os.name != "nt":
            import signal

            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):  # pragma: no cover - race
                pass
        process.kill()

    def _run_on_host(self, root: str) -> tuple[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMP", "TEMP"}
        }
        with tempfile.TemporaryDirectory(prefix="evoagent-verify-") as temp:
            env["TMPDIR"] = temp
            if os.name == "nt":  # pragma: no cover - POSIX CI
                command = ["cmd", "/c", self.test_command]
            else:
                # Apply limits inside the shell child (thread-safe, unlike preexec_fn).
                # The whole process group is killed on timeout, so no `exec` is needed
                # and multi-statement test commands keep their shell semantics.
                # RLIMIT_NPROC/`ulimit -u` is intentionally omitted: it is per-UID, so it
                # would count the service's other processes and cause spurious fork
                # failures. Fork-bomb containment is a container-mode guarantee only.
                prelude = "ulimit -t %d 2>/dev/null; ulimit -f %d 2>/dev/null; %s" % (
                    max(1, self.timeout_seconds),
                    self.max_file_mb * 1024,
                    self.test_command,
                )
                command = ["/bin/sh", "-c", prelude]
            returncode, detail, timed_out = self._execute(command, cwd=root, env=env)
        if timed_out:
            return "timeout", "verification exceeded %d seconds" % self.timeout_seconds
        if returncode is None:
            return "error", detail
        return ("passed" if returncode == 0 else "failed"), detail

    def _run_in_container(self, root: str) -> tuple[str, str]:
        name = "evoagent-verify-%s" % uuid.uuid4().hex[:12]
        file_bytes = self.max_file_mb * 1024 * 1024
        deadline = time.monotonic() + self.timeout_seconds
        try:
            reconcile_sandboxes(timeout_seconds=min(10.0, max(0.001, deadline - time.monotonic())))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return "error", safe_exception_summary(exc, "container reconciliation failed")
        work_tmpfs = "/work:rw,nosuid,nodev,size=%dm,uid=65534,gid=65534,mode=0700" % self.memory_mb
        command = sandbox_command(
            self.container_image,
            name,
            self.timeout_seconds,
            [
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=%dm,mode=1777" % self.memory_mb,
                "--pids-limit",
                str(self.pids_limit),
                "--memory",
                "%dm" % self.memory_mb,
                "--cpus",
                str(self.cpus),
                "--ulimit",
                "fsize=%d:%d" % (file_bytes, file_bytes),
                "--ulimit",
                "nofile=1024:1024",
                "--tmpfs",
                work_tmpfs,
            ],
        )
        copy = ["docker", "exec", "-i", "--workdir", "/work", "--user", "65534:65534"]
        copy += [name, "tar", "-xf", "-", "--no-same-owner"]
        cleanup_required = False
        try:
            for setup in (command, copy):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", "verification exceeded %d seconds" % self.timeout_seconds
                try:
                    # The daemon may create the container before the CLI reports success.
                    cleanup_required = True
                    if setup is copy:
                        returncode = self._copy_worktree(root, copy, deadline)
                    else:
                        returncode = subprocess.run(
                            setup,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=remaining,
                            check=False,
                        ).returncode
                except subprocess.TimeoutExpired:
                    return "timeout", "verification exceeded %d seconds" % self.timeout_seconds
                except OSError as exc:
                    return "error", safe_exception_summary(exc, "container preparation failed")
                if returncode:
                    return "error", "container preparation failed"
            execute = ["docker", "exec", "--workdir", "/work", "--user", "65534:65534"]
            execute += [name, "sh", "-c", self.test_command]
            returncode, detail, timed_out = self._execute(
                execute,
                cwd=None,
                env=None,
                timeout_seconds=deadline - time.monotonic(),
            )
            if timed_out:
                return "timeout", "verification exceeded %d seconds" % self.timeout_seconds
            if returncode is None:
                return "error", detail
            return ("passed" if returncode == 0 else "failed"), detail
        finally:
            if cleanup_required:
                try:
                    cleanup = remove_sandbox(name)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    metrics.inc("repair_container_cleanup_failures_total")
                    raise RuntimeError("repair container cleanup failed") from exc
                if not cleanup:
                    metrics.inc("repair_container_cleanup_failures_total")
                    raise RuntimeError("repair container cleanup failed")

    def _copy_worktree(self, root: str, command: list[str], deadline: float) -> int:
        # Stream from the client: docker cp cannot reliably write to read-only-rootfs tmpfs.
        source = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys, tarfile\n"
                "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:\n"
                "    archive.add(sys.argv[1], arcname='.')\n",
                root,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        assert source.stdout is not None
        try:
            result = subprocess.run(
                command,
                stdin=source.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(0.0, deadline - time.monotonic()),
                check=False,
            )
            if result.returncode:
                return result.returncode
            # A valid partial archive can extract successfully after its producer fails.
            return source.wait(timeout=max(0.0, deadline - time.monotonic()))
        finally:
            try:
                if source.poll() is None:
                    self._terminate(source)
                source.wait(timeout=5)
            finally:
                source.stdout.close()

    def verify_archive(self, archive: bytes, files: dict[str, str]) -> dict:
        """Verify changed files inside an isolated copy of the complete repository."""
        if not isinstance(archive, bytes) or len(archive) > self.max_archive_uncompressed_bytes:
            raise ValueError("repository archive exceeds extraction limits")
        with tempfile.TemporaryDirectory(prefix="evoagent-repair-") as root:
            with zipfile.ZipFile(BytesIO(archive)) as bundle:
                members = bundle.infolist()
                if (
                    len(members) > self.MAX_ARCHIVE_MEMBERS
                    or sum(member.file_size for member in members)
                    > self.max_archive_uncompressed_bytes
                ):
                    raise ValueError("repository archive exceeds extraction limits")
                for member in members:
                    normalized = os.path.normpath(member.filename).replace("\\", "/")
                    if normalized.startswith("../") or normalized.startswith("/"):
                        raise ValueError("repository archive contains an unsafe path")
                    target = os.path.abspath(os.path.join(root, normalized))
                    if not target.startswith(os.path.abspath(root) + os.sep):
                        raise ValueError("repository archive escapes the sandbox")
                    bundle.extract(member, root)
            entries = [item for item in os.scandir(root) if item.is_dir()]
            worktree = entries[0].path if len(entries) == 1 else root
            for path, content in files.items():
                target = os.path.abspath(os.path.join(worktree, path))
                if not target.startswith(os.path.abspath(worktree) + os.sep):
                    raise ValueError("repair path escapes the repository")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
            compile_result = self.verify_contents(files)
            if not compile_result["passed"]:
                return compile_result
            test_result = self.verify_worktree(worktree)
            checks = compile_result["checks"] + test_result["checks"]
            return {
                "passed": compile_result["passed"] and test_result["passed"],
                "checks": checks,
                **(
                    {"attestation": test_result["attestation"]}
                    if isinstance(test_result.get("attestation"), dict)
                    else {}
                ),
                "duration_seconds": round(
                    compile_result.get("duration_seconds", 0)
                    + test_result.get("duration_seconds", 0),
                    4,
                ),
            }
