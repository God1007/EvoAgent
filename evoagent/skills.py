"""Versioned skill registry with manifest validation and process isolation."""

import ast
import builtins
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass

from .models import Finding
from .reviewer import Reviewer

FORBIDDEN_IMPORTS = {
    "ctypes",
    "multiprocessing",
    "socket",
    "subprocess",
    "urllib",
    "http",
    "ftplib",
    "telnetlib",
}
MAX_SKILL_SOURCE_BYTES = 1024 * 1024
MAX_SKILL_OUTPUT_BYTES = 1024 * 1024
MAX_SKILL_ERROR_BYTES = 64 * 1024
_SKILL_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")


@dataclass
class SkillInfo:
    name: str
    version: str
    description: str
    source: str
    sandboxed: bool = False
    permissions: tuple = ()


class SandboxedSkillReviewer(Reviewer):
    def __init__(
        self,
        name: str,
        module_path: str,
        timeout_seconds: int = 30,
        memory_mb: int = 256,
        container_image: str = "",
        source: str = "",
        source_sha256: str = "",
    ):
        self.name = name
        self.module_path = os.path.abspath(module_path)
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.container_image = container_image
        self.source = source
        self.source_sha256 = source_sha256
        self.runner = os.path.join(os.path.dirname(__file__), "skill_runner.py")

    def review(self, diff, parsed) -> list[Finding]:
        payload = {
            "diff": diff,
            "parsed": {
                "files": parsed.files,
                "added_lines": [
                    {"path": line.path, "line": line.line, "content": line.content}
                    for line in parsed.added_lines
                ],
            },
            "memory_mb": self.memory_mb,
            "skill_source": self.source,
            "skill_sha256": self.source_sha256,
        }
        with tempfile.TemporaryDirectory(prefix="evoagent-skill-") as workdir:
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMP", "TEMP"}
            }
            env["PYTHONHASHSEED"] = "0"
            command = [sys.executable, "-I", self.runner, self.module_path]
            if self.container_image:
                package_root = os.path.dirname(os.path.dirname(self.runner))
                command = [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "64",
                    "--memory",
                    "%dm" % self.memory_mb,
                    "--cpus",
                    "0.5",
                    "-v",
                    package_root + ":/app:ro",
                    self.container_image,
                    "python",
                    "-I",
                    "/app/evoagent/skill_runner.py",
                    "/skill/" + os.path.basename(self.module_path),
                ]
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                try:
                    result = subprocess.run(
                        command,
                        input=json.dumps(payload).encode("utf-8"),
                        stdout=stdout,
                        stderr=stderr,
                        cwd=workdir,
                        env=env,
                        timeout=self.timeout_seconds,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("skill %s exceeded its time limit" % self.name) from exc
                stdout.seek(0)
                output = stdout.read(MAX_SKILL_OUTPUT_BYTES + 1)
                stderr.seek(0)
                error_output = stderr.read(MAX_SKILL_ERROR_BYTES + 1)
        if len(output) > MAX_SKILL_OUTPUT_BYTES:
            raise RuntimeError("skill %s exceeded its output limit" % self.name)
        if result.returncode != 0:
            raise RuntimeError(
                "skill %s failed in sandbox: %s"
                % (self.name, error_output[-1000:].decode("utf-8", errors="replace"))
            )
        try:
            values = json.loads(output.decode("utf-8"))
            return [Finding.from_dict(value) for value in values]
        except Exception as exc:
            raise RuntimeError("skill %s returned invalid output" % self.name) from exc


class SkillRegistry:
    def __init__(
        self,
        skills_dir: str,
        sandbox: bool = True,
        timeout_seconds: int = 30,
        memory_mb: int = 256,
        signing_key: str = "",
        container_image: str = "",
        require_container: bool = False,
    ):
        self.skills_dir = skills_dir
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.signing_key = signing_key.encode("utf-8")
        self.container_image = container_image
        self.require_container = require_container
        self._skills: dict[str, Reviewer] = {}
        self._info: dict[str, SkillInfo] = {}
        self._lock = threading.RLock()

    def register(
        self,
        name: str,
        reviewer: Reviewer,
        version: str = "1.0.0",
        description: str = "",
        source: str = "builtin",
        sandboxed: bool = False,
        permissions: tuple = (),
    ) -> None:
        if not name.replace("-", "_").isidentifier():
            raise ValueError("invalid skill name: %s" % name)
        with self._lock:
            existing = self._info.get(name)
            if existing is not None and (
                existing.sandboxed != sandboxed or (not sandboxed and existing.source != source)
            ):
                raise ValueError("skill name collides across trust domains: %s" % name)
            self._skills[name] = reviewer
            self._info[name] = SkillInfo(name, version, description, source, sandboxed, permissions)

    def reviewers(self) -> list[Reviewer]:
        with self._lock:
            return list(self._skills.values())

    def list(self) -> list[dict]:
        with self._lock:
            values = []
            for item in self._info.values():
                value = vars(item).copy()
                value["permissions"] = list(value["permissions"])
                values.append(value)
            return values

    def reload(self) -> builtins.list[dict]:
        if not os.path.isdir(self.skills_dir):
            os.makedirs(self.skills_dir, exist_ok=True)
            self._replace_dynamic({}, {})
            return self.list()
        root = os.path.realpath(self.skills_dir)
        candidates: dict[str, Reviewer] = {}
        candidate_info: dict[str, SkillInfo] = {}
        for entry in sorted(os.scandir(self.skills_dir), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            if entry.is_symlink():
                raise ValueError("skill directories must not be symbolic links: %s" % entry.name)
            manifest_path = os.path.join(entry.path, "skill.json")
            if not os.path.isfile(manifest_path):
                continue
            if os.path.islink(manifest_path):
                raise ValueError("skill manifests must not be symbolic links: %s" % entry.name)
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            if not isinstance(manifest, dict):
                raise ValueError("skill manifest must be a JSON object: %s" % entry.name)
            for field in ("name", "version", "entrypoint", "sha256", "permissions"):
                if field not in manifest:
                    raise ValueError("skill manifest is missing %s" % field)
            skill_root = os.path.realpath(entry.path)
            if not skill_root.startswith(root + os.sep):
                raise ValueError("skill directory escapes the configured root: %s" % entry.name)
            module_path = os.path.realpath(
                os.path.join(skill_root, str(manifest.get("entrypoint", "")))
            )
            if not module_path.startswith(skill_root + os.sep):
                raise ValueError("skill entrypoint escapes its directory: %s" % entry.name)
            if not os.path.isfile(module_path):
                raise ValueError("skill entrypoint is not a regular file: %s" % entry.name)
            with open(module_path, "rb") as handle:
                source = handle.read(MAX_SKILL_SOURCE_BYTES + 1)
            if len(source) > MAX_SKILL_SOURCE_BYTES:
                raise ValueError("skill source exceeds the 1 MiB limit: %s" % entry.name)
            self._validate_manifest(manifest, module_path, source)
            try:
                source_text = source.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("skill source must be UTF-8: %s" % entry.name) from exc
            name = str(manifest["name"])
            if name in candidates:
                raise ValueError("duplicate dynamic skill name: %s" % name)
            reviewer = SandboxedSkillReviewer(
                name,
                module_path,
                self.timeout_seconds,
                self.memory_mb,
                self.container_image,
                source_text,
                hashlib.sha256(source).hexdigest(),
            )
            if not self.sandbox:
                raise ValueError("dynamic skills require EVOAGENT_SKILL_SANDBOX=true")
            candidates[name] = reviewer
            candidate_info[name] = SkillInfo(
                name,
                str(manifest["version"]),
                str(manifest.get("description", "Dynamic review skill")),
                module_path,
                True,
                tuple(manifest.get("permissions", [])),
            )
        if candidates and self.require_container and not self.container_image:
            raise ValueError(
                "dynamic skills require EVOAGENT_SKILL_CONTAINER_IMAGE when "
                "EVOAGENT_SKILL_REQUIRE_CONTAINER=true"
            )
        self._replace_dynamic(candidates, candidate_info)
        return self.list()

    def _replace_dynamic(
        self,
        candidates: dict[str, Reviewer],
        candidate_info: dict[str, SkillInfo],
    ) -> None:
        with self._lock:
            protected = {name for name, info in self._info.items() if not info.sandboxed}
            collisions = sorted(protected.intersection(candidates))
            if collisions:
                raise ValueError(
                    "dynamic skills collide with trusted contributions: %s" % ", ".join(collisions)
                )
            retained_skills = {
                name: reviewer
                for name, reviewer in self._skills.items()
                if not self._info[name].sandboxed
            }
            retained_info = {name: info for name, info in self._info.items() if not info.sandboxed}
            self._skills = {**retained_skills, **candidates}
            self._info = {**retained_info, **candidate_info}

    def _validate_manifest(self, manifest: dict, module_path: str, source: bytes) -> None:
        name = str(manifest["name"])
        version = str(manifest["version"])
        if not name.replace("-", "_").isidentifier():
            raise ValueError("invalid skill name: %s" % name)
        if not _SKILL_VERSION.fullmatch(version):
            raise ValueError("skill %s has invalid semantic version: %s" % (name, version))
        if not isinstance(manifest["permissions"], list):
            raise ValueError("skill permissions must be an array: %s" % name)
        if manifest["permissions"]:
            raise ValueError("review skills currently receive no host permissions")
        if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"])):
            raise ValueError("skill manifest has an invalid checksum: %s" % name)
        digest = hashlib.sha256(source).hexdigest()
        if not hmac.compare_digest(digest, str(manifest["sha256"])):
            raise ValueError("skill checksum mismatch: %s" % manifest["name"])
        if self.signing_key:
            expected = hmac.new(self.signing_key, source, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(manifest.get("signature", ""))):
                raise ValueError("skill signature mismatch: %s" % manifest["name"])
        tree = ast.parse(source, filename=module_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [item.name.split(".")[0] for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            blocked = FORBIDDEN_IMPORTS.intersection(names)
            if blocked:
                raise ValueError(
                    "skill %s imports forbidden modules: %s"
                    % (manifest["name"], ", ".join(sorted(blocked)))
                )
