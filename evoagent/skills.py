"""Versioned skill registry with manifest validation and process isolation."""

import ast
import builtins
import hashlib
import hmac
import json
import os
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
    ):
        self.name = name
        self.module_path = os.path.abspath(module_path)
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.container_image = container_image
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
        }
        with tempfile.TemporaryDirectory(prefix="evoagent-skill-") as workdir:
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMP", "TEMP"}
            }
            env["PYTHONHASHSEED"] = "0"
            try:
                command = [sys.executable, "-I", self.runner, self.module_path]
                if self.container_image:
                    package_root = os.path.dirname(os.path.dirname(self.runner))
                    skill_root = os.path.dirname(self.module_path)
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
                        "-v",
                        skill_root + ":/skill:ro",
                        self.container_image,
                        "python",
                        "-I",
                        "/app/evoagent/skill_runner.py",
                        "/skill/" + os.path.basename(self.module_path),
                    ]
                result = subprocess.run(
                    command,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    cwd=workdir,
                    env=env,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("skill %s exceeded its time limit" % self.name) from exc
        if result.returncode != 0:
            raise RuntimeError(
                "skill %s failed in sandbox: %s" % (self.name, result.stderr[-1000:])
            )
        try:
            values = json.loads(result.stdout)
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
    ):
        self.skills_dir = skills_dir
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.signing_key = signing_key.encode("utf-8")
        self.container_image = container_image
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
            return self.list()
        for entry in os.scandir(self.skills_dir):
            if not entry.is_dir():
                continue
            manifest_path = os.path.join(entry.path, "skill.json")
            if not os.path.isfile(manifest_path):
                continue
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            module_path = os.path.abspath(os.path.join(entry.path, manifest.get("entrypoint", "")))
            if not module_path.startswith(os.path.abspath(entry.path) + os.sep):
                raise ValueError("skill entrypoint escapes its directory: %s" % entry.name)
            self._validate_manifest(manifest, module_path)
            reviewer = SandboxedSkillReviewer(
                manifest["name"],
                module_path,
                self.timeout_seconds,
                self.memory_mb,
                self.container_image,
            )
            if not self.sandbox:
                raise ValueError("dynamic skills require EVOAGENT_SKILL_SANDBOX=true")
            self.register(
                manifest["name"],
                reviewer,
                manifest["version"],
                manifest.get("description", "Dynamic review skill"),
                module_path,
                True,
                tuple(manifest.get("permissions", [])),
            )
        return self.list()

    def _validate_manifest(self, manifest: dict, module_path: str) -> None:
        for field in ("name", "version", "entrypoint", "sha256", "permissions"):
            if field not in manifest:
                raise ValueError("skill manifest is missing %s" % field)
        if manifest["permissions"]:
            raise ValueError("review skills currently receive no host permissions")
        with open(module_path, "rb") as handle:
            source = handle.read()
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
