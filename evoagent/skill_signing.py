"""Sign one dynamic Skill manifest for immutable deployment."""

import argparse
import hashlib
import hmac
import json
import os
import stat
import tempfile

from .json_boundary import strict_json_loads
from .skills import (
    MAX_SKILL_ENTRYPOINT_CHARS,
    MAX_SKILL_MANIFEST_BYTES,
    MAX_SKILL_SOURCE_BYTES,
    _skill_signature_payload,
)


def sign_manifest(manifest_path: str, key: str) -> str:
    if len(key.encode("utf-8")) < 32:
        raise ValueError("EVOAGENT_SKILL_SIGNING_KEY must contain at least 32 bytes")
    manifest_path = os.path.abspath(manifest_path)
    if os.path.islink(manifest_path) or not os.path.isfile(manifest_path):
        raise ValueError("skill manifest must be a regular non-symlink file")
    with open(manifest_path, "rb") as handle:
        content = handle.read(MAX_SKILL_MANIFEST_BYTES + 1)
    if len(content) > MAX_SKILL_MANIFEST_BYTES:
        raise ValueError("skill manifest exceeds the 64 KiB limit")
    manifest = strict_json_loads(content)
    if not isinstance(manifest, dict):
        raise ValueError("skill manifest must be a JSON object")
    entrypoint = manifest.get("entrypoint")
    if (
        not isinstance(entrypoint, str)
        or not entrypoint
        or len(entrypoint) > MAX_SKILL_ENTRYPOINT_CHARS
    ):
        raise ValueError("skill manifest has an invalid entrypoint")
    root = os.path.dirname(manifest_path)
    source_path = os.path.realpath(os.path.join(root, entrypoint))
    if not source_path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(
        source_path
    ):
        raise ValueError("skill entrypoint escapes its directory or is unavailable")
    with open(source_path, "rb") as handle:
        source = handle.read(MAX_SKILL_SOURCE_BYTES + 1)
    if len(source) > MAX_SKILL_SOURCE_BYTES:
        raise ValueError("skill source exceeds the 1 MiB limit")
    manifest["sha256"] = hashlib.sha256(source).hexdigest()
    manifest["signature"] = hmac.new(
        key.encode("utf-8"), _skill_signature_payload(manifest), hashlib.sha256
    ).hexdigest()
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_SKILL_MANIFEST_BYTES:
        raise ValueError("signed skill manifest exceeds the 64 KiB limit")
    mode = stat.S_IMODE(os.stat(manifest_path).st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=".skill-sign-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, manifest_path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return manifest["signature"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign an EvoAgent Skill manifest")
    parser.add_argument("manifest", help="path to skill.json")
    args = parser.parse_args(argv)
    key = os.getenv("EVOAGENT_SKILL_SIGNING_KEY", "")
    try:
        signature = sign_manifest(args.manifest, key)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(signature)
    return 0


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
