"""Minimal JSON protocol used by the isolated skill subprocess."""

import hashlib
import hmac
import json
import os
import re
import sys
from typing import Any

SKILL_PROTOCOL_VERSION = 1


def _set_resource_limit(resource_module, limit_name: str, value: int) -> bool:
    """Apply a best-effort POSIX limit without breaking unsupported platforms."""
    limit = getattr(resource_module, limit_name, None)
    if limit is None:
        return False
    try:
        resource_module.setrlimit(limit, (value, value))
    except (OSError, ValueError):
        # macOS exposes RLIMIT_AS but rejects finite values on some versions.
        # The parent still enforces a wall-clock timeout and the audit hook;
        # production containers enforce memory independently.
        return False
    return True


def main() -> None:
    module_path = os.path.abspath(sys.argv[1])
    package_dir = os.path.dirname(os.path.abspath(__file__))
    package_root = os.path.dirname(package_dir)
    sys.path.insert(0, package_root)
    payload = json.load(sys.stdin)
    protocol_version = payload.get("protocol_version")
    if type(protocol_version) is not int or protocol_version != SKILL_PROTOCOL_VERSION:
        raise RuntimeError("unsupported skill protocol version")
    source = payload.get("skill_source")
    expected_sha256 = str(payload.get("skill_sha256", ""))
    if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("skill snapshot is missing or invalid")
    actual_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise RuntimeError("skill snapshot checksum mismatch")
    memory_mb = payload.get("memory_mb")
    timeout_seconds = payload.get("timeout_seconds")
    if any(type(value) is not int or value <= 0 for value in (memory_mb, timeout_seconds)):
        raise RuntimeError("invalid skill resource limits")

    if os.name != "nt":
        import resource

        memory = memory_mb * 1024 * 1024
        _set_resource_limit(resource, "RLIMIT_AS", memory)
        _set_resource_limit(resource, "RLIMIT_CPU", timeout_seconds)
        _set_resource_limit(resource, "RLIMIT_FSIZE", 8 * 1024 * 1024)
        _set_resource_limit(resource, "RLIMIT_NOFILE", 64)

    allowed_roots = {
        os.path.realpath(os.getcwd()),
        os.path.realpath(sys.prefix),
        os.path.realpath(sys.base_prefix),
        os.path.realpath(package_dir),
    }

    def audit(event, args):
        if event.startswith(
            (
                "socket.",
                "subprocess.",
                "os.system",
                "os.fork",
                "os.exec",
                "os.posix_spawn",
                "os.kill",
                "os.remove",
                "os.rename",
                "os.rmdir",
                "os.mkdir",
                "os.chmod",
                "os.chown",
                "os.link",
                "os.symlink",
                "os.truncate",
                "ctypes.dlopen",
            )
        ):
            raise PermissionError("operation blocked by skill sandbox")
        if event == "open" and args:
            if isinstance(args[0], int):
                return
            path = os.path.realpath(os.fsdecode(args[0]))
            mode = args[1] if len(args) > 1 else "r"
            write_requested = (
                bool(mode & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
                if isinstance(mode, int)
                else any(flag in str(mode) for flag in ("w", "a", "x", "+"))
            )
            if write_requested:
                raise PermissionError("file writes are blocked by skill sandbox")
            if not any(path == root or path.startswith(root + os.sep) for root in allowed_roots):
                raise PermissionError("file access blocked by skill sandbox")

    sys.addaudithook(audit)
    from evoagent.diff_parser import ParsedDiff

    namespace: dict[str, Any] = {
        "__name__": "evoagent_isolated_skill",
        "__file__": module_path,
        "__package__": None,
    }
    exec(compile(source, module_path, "exec"), namespace)
    parsed = ParsedDiff.from_dict(payload["parsed"])
    create_skill = namespace.get("create_skill")
    if not callable(create_skill):
        raise RuntimeError("skill module does not export create_skill")
    findings = create_skill().review(payload["diff"], parsed)
    json.dump([item.to_dict() for item in findings], sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
