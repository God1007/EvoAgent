"""Shared CLI lifecycle for disposable Linux sandboxes, not a Docker SDK."""

import math
import re
import subprocess
import time

# Covers bounded producer teardown and explicit cleanup if the owner disappears.
SANDBOX_EXIT_GRACE_SECONDS = 15
MAX_RECONCILE_SANDBOXES = 64
_NAME = re.compile(r"evoagent-(?:verify|skill)-[0-9a-f]{12}\Z")
_EXPIRES_LABEL = "com.evoagent.sandbox.expires-at"


def sandbox_command(image: str, name: str, timeout_seconds: int, limits: list[str]) -> list[str]:
    if not _NAME.fullmatch(name) or not image or image.startswith("-"):
        raise ValueError("invalid sandbox identity")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("sandbox deadline must be positive")
    # PID 1 is the trusted image's sleep binary, never a shell or untrusted code.
    # Its separate UID denies untrusted exec code same-user ptrace/memory access.
    # Linux ends the whole PID namespace when it exits; Docker owns removal even
    # after our process dies. Explicitly disable daemon init and image healthchecks.
    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--init=false",
        "--no-healthcheck",
        "--entrypoint",
        "/bin/sleep",
        "--pull",
        "never",
        "--label",
        "%s=%d" % (_EXPIRES_LABEL, int(time.time()) + timeout_seconds + SANDBOX_EXIT_GRACE_SECONDS),
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "65533:65533",
        "--workdir",
        "/",
    ]
    # Docker client configuration can otherwise inject proxy credentials.
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        command += ["--env", proxy + "=", "--env", proxy.lower() + "="]
    return command + limits + [image, str(timeout_seconds + SANDBOX_EXIT_GRACE_SECONDS)]


def remove_sandbox(
    name: str, env: dict[str, str] | None = None, timeout_seconds: float = 10
) -> bool:
    """Remove only the generated name, accepting confirmed automatic removal.

    A nonzero rm is not sufficient evidence of absence: a successful daemon
    query must confirm it. Both attempts share the existing ten-second budget.
    """
    if not _NAME.fullmatch(name):
        raise ValueError("refusing to remove a non-sandbox name")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("sandbox cleanup timeout must be positive and finite")
    deadline = time.monotonic() + timeout_seconds
    result = subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
        check=False,
        env=env,
    )
    if result.returncode == 0:
        return True
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    present = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=" + name, "--format", "{{.ID}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=remaining,
        check=False,
        text=True,
        env=env,
    )
    # The substring filter is deliberately conservative: any match means that
    # absence is unproven. Never delete anything discovered by this query.
    return (
        present.returncode == 0 and isinstance(present.stdout, str) and not present.stdout.strip()
    )


def reconcile_sandboxes(
    env: dict[str, str] | None = None,
    *,
    now: int | None = None,
    timeout_seconds: float = 10,
) -> int:
    """Remove only labelled sandboxes whose trusted wall-clock deadline passed."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("sandbox reconciliation timeout must be positive and finite")
    if now is None:
        now = int(time.time())
    if type(now) is not int or now < 0:
        raise ValueError("sandbox reconciliation time must be a non-negative integer")
    deadline = time.monotonic() + timeout_seconds
    inventory = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=" + _EXPIRES_LABEL,
            "--format",
            '{{.Names}}\t{{.Label "%s"}}' % _EXPIRES_LABEL,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout_seconds,
        check=False,
        text=True,
        env=env,
    )
    if inventory.returncode or not isinstance(inventory.stdout, str):
        raise RuntimeError("sandbox inventory failed")
    lines = inventory.stdout.splitlines()
    # ponytail: fail closed above one bounded page; add daemon-side pagination
    # only if a real deployment can accumulate more than this many sandboxes.
    if len(lines) > MAX_RECONCILE_SANDBOXES:
        raise RuntimeError("sandbox inventory exceeds the reconciliation limit")
    expired = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 2 or not _NAME.fullmatch(parts[0]) or not parts[1].isdigit():
            raise RuntimeError("sandbox inventory contains an invalid identity")
        if int(parts[1]) <= now:
            expired.append(parts[0])
    for name in expired:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not remove_sandbox(name, env, remaining):
            raise RuntimeError("expired sandbox cleanup failed")
    return len(expired)
