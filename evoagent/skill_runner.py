"""Minimal JSON protocol used by the isolated skill subprocess."""

import json
import os
import sys


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
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, package_root)
    payload = json.load(sys.stdin)

    if os.name != "nt":
        import resource

        memory = int(payload.get("memory_mb", 256)) * 1024 * 1024
        _set_resource_limit(resource, "RLIMIT_AS", memory)
        _set_resource_limit(resource, "RLIMIT_CPU", 30)

    allowed_roots = {
        os.path.realpath(os.path.dirname(module_path)),
        os.path.realpath(os.getcwd()),
        os.path.realpath(sys.prefix),
        os.path.realpath(sys.base_prefix),
        os.path.realpath(package_root),
    }

    def audit(event, args):
        if event.startswith(("socket.", "subprocess.", "os.system")):
            raise PermissionError("operation blocked by skill sandbox")
        if event == "open" and args:
            if isinstance(args[0], int):
                return
            path = os.path.realpath(os.fsdecode(args[0]))
            if not any(path == root or path.startswith(root + os.sep) for root in allowed_roots):
                raise PermissionError("file access blocked by skill sandbox")

    sys.addaudithook(audit)
    import importlib.util

    from evoagent.diff_parser import ParsedDiff
    from evoagent.models import ChangedLine

    spec = importlib.util.spec_from_file_location("evoagent_isolated_skill", module_path)
    if not spec or not spec.loader:
        raise RuntimeError("invalid skill module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parsed_value = payload["parsed"]
    parsed = ParsedDiff(
        parsed_value["files"], [ChangedLine(**item) for item in parsed_value["added_lines"]]
    )
    findings = module.create_skill().review(payload["diff"], parsed)
    json.dump([item.to_dict() for item in findings], sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
