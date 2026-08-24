"""Versioned skill registry with manifest validation and process isolation."""

import ast
import builtins
import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from itertools import islice
from typing import cast

from .diff_parser import ParsedDiff
from .json_boundary import strict_json_loads
from .metrics import metrics
from .models import FINDING_TEXT_LIMITS, Finding, Severity
from .reviewer import (
    MAX_REVIEWER_FINDINGS,
    MAX_REVIEWER_NAME_CHARS,
    Reviewer,
    valid_reviewer_name,
)
from .skill_runner import SKILL_PROTOCOL_VERSION

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
MAX_SKILL_MANIFEST_BYTES = 64 * 1024
MAX_SKILL_OUTPUT_BYTES = 1024 * 1024
MAX_SKILL_ERROR_BYTES = 64 * 1024
MAX_SKILL_VERSION_CHARS = 100
MAX_SKILL_DESCRIPTION_CHARS = 1000
MAX_SKILL_ENTRYPOINT_CHARS = 500
MAX_SKILL_SOURCE_LABEL_CHARS = 200
# ponytail: fixed plugin ceiling; raise it only when 32 deployed Skills prove insufficient.
MAX_SKILL_ENTRIES = 32
_SKILL_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_SKILL_FINDING_FIELDS = {
    "rule_id",
    "severity",
    "title",
    "explanation",
    "path",
    "line",
    "evidence",
    "fix",
    "test",
    "confidence",
    "fingerprint",
}


def _skill_signature_payload(manifest: dict) -> bytes:
    signed = dict(manifest)
    signed.pop("signature", None)
    return json.dumps(
        signed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_skill_metadata(name: str, version: str, description: str) -> None:
    if not all(isinstance(value, str) for value in (name, version, description)):
        raise ValueError("skill metadata must be strings")
    if not name.replace("-", "_").isidentifier() or len(name) > MAX_REVIEWER_NAME_CHARS:
        raise ValueError("invalid skill name")
    if len(version) > MAX_SKILL_VERSION_CHARS or not _SKILL_VERSION.fullmatch(version):
        raise ValueError("skill has an invalid semantic version")
    if len(description) > MAX_SKILL_DESCRIPTION_CHARS or (
        description and not description.isprintable()
    ):
        raise ValueError("skill %s has invalid description" % name)


def _parse_skill_findings(values: object, parsed: ParsedDiff) -> list[Finding]:
    if not isinstance(values, list) or len(values) > MAX_REVIEWER_FINDINGS:
        raise ValueError("invalid finding list")
    source_lines = {(line.path, line.line): line.content for line in parsed.added_lines}
    findings = []
    for value in values:
        if not isinstance(value, dict) or set(value) != _SKILL_FINDING_FIELDS:
            raise ValueError("invalid finding fields")
        if value["severity"] not in {item.value for item in Severity}:
            raise ValueError("invalid finding severity")
        finding = Finding.from_dict(value)
        if value["fingerprint"] != finding.fingerprint():
            raise ValueError("invalid finding fingerprint")
        source = source_lines.get((finding.path, finding.line))
        if source is None or not finding.evidence.strip() or finding.evidence.strip() not in source:
            raise ValueError("invalid finding evidence location")
        for field in ("title", "explanation", "evidence", "fix", "test"):
            text = getattr(finding, field)
            if not text.strip() or len(text) > FINDING_TEXT_LIMITS[field]:
                raise ValueError("invalid finding text")
        findings.append(finding)
    return findings


def _run_bounded(
    command: list[str],
    payload: bytes,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, bytes, bytes, bool, bool]:
    """Run a Skill while bounding its output during execution, not after it."""
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload)
        input_file.seek(0)
        process = subprocess.Popen(
            command,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=os.name != "nt",
        )
        output = bytearray()
        error = bytearray()
        exceeded = threading.Event()

        def terminate() -> None:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows has no stdlib process-tree kill
                    process.kill()
            except OSError:
                pass

        def drain(stream, target: bytearray, limit: int) -> None:
            while chunk := stream.read(65536):
                remaining = max(0, limit + 1 - len(target))
                target.extend(chunk[:remaining])
                if len(target) > limit or len(chunk) > remaining:
                    exceeded.set()
                    terminate()
                    return

        threads = [
            threading.Thread(
                target=drain,
                args=(process.stdout, output, MAX_SKILL_OUTPUT_BYTES),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, error, MAX_SKILL_ERROR_BYTES),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable OS process
                returncode = -1
        terminate()  # one-shot Skills may not leave descendants behind
        drain_deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(max(0.0, drain_deadline - time.monotonic()))
        timed_out = timed_out or any(thread.is_alive() for thread in threads)
        return returncode, bytes(output), bytes(error), timed_out, exceeded.is_set()


def resolve_container_image(image: str) -> str:
    """Resolve a preloaded Docker image reference to its immutable local ID."""
    if not image:
        return ""
    if image.startswith("-"):
        raise ValueError("container image is invalid")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("container runtime is unavailable") from exc
    image_id = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("container daemon or image is unavailable")
    return image_id


@dataclass
class SkillInfo:
    name: str
    version: str
    description: str
    source: str
    sandboxed: bool = False
    permissions: tuple = ()
    protocol_version: int | None = None


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
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (timeout_seconds, memory_mb)
        ):
            raise ValueError("skill sandbox timeout and memory limits must be positive")
        self.name = name
        self.module_path = os.path.abspath(module_path)
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb
        self.container_image = container_image
        self.source = source
        self.source_sha256 = source_sha256
        self.runner = os.path.join(os.path.dirname(__file__), "skill_runner.py")

    def review(self, diff, parsed) -> list[Finding]:
        metrics.inc("skill_runs_total")
        try:
            with metrics.latency("skill_execution"):
                findings = self._review(diff, parsed)
        except Exception:
            metrics.inc("skill_failures_total")
            raise
        metrics.inc("skill_successes_total")
        metrics.inc("skill_findings_total", len(findings))
        return findings

    def _review(self, diff: str, parsed: ParsedDiff) -> list[Finding]:
        payload = {
            "protocol_version": SKILL_PROTOCOL_VERSION,
            "diff": diff,
            "parsed": parsed.to_dict(),
            "memory_mb": self.memory_mb,
            "timeout_seconds": self.timeout_seconds,
            "skill_source": self.source,
            "skill_sha256": self.source_sha256,
        }
        container_name = "evoagent-skill-%s" % uuid.uuid4().hex[:12] if self.container_image else ""
        with tempfile.TemporaryDirectory(prefix="evoagent-skill-") as workdir:
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "TMP", "TEMP"}
            }
            env["PYTHONHASHSEED"] = "0"
            command = [sys.executable, "-I", self.runner, self.module_path]
            if self.container_image:
                command = [
                    "docker",
                    "run",
                    "-i",
                    "--pull",
                    "never",
                    "--name",
                    container_name,
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
                ]
                if os.name != "nt":
                    command += ["--user", "65534:65534"]
                command += [
                    self.container_image,
                    "python",
                    "-I",
                    "/app/evoagent/skill_runner.py",
                    "/skill/" + os.path.basename(self.module_path),
                ]
            try:
                returncode, output, error_output, timed_out, output_exceeded = _run_bounded(
                    command,
                    json.dumps(payload).encode("utf-8"),
                    workdir,
                    env,
                    self.timeout_seconds,
                )
            finally:
                if container_name:
                    try:
                        cleanup = subprocess.run(
                            ["docker", "rm", "-f", container_name],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            check=False,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        metrics.inc("skill_container_cleanup_failures_total")
                        raise RuntimeError("skill container cleanup failed") from exc
                    if cleanup.returncode:
                        metrics.inc("skill_container_cleanup_failures_total")
                        raise RuntimeError("skill container cleanup failed")
        if output_exceeded:
            metrics.inc("skill_output_limit_rejections_total")
            raise RuntimeError("skill %s exceeded its output limit" % self.name)
        if timed_out:
            metrics.inc("skill_timeouts_total")
            raise RuntimeError("skill %s exceeded its time limit" % self.name)
        if returncode != 0:
            metrics.inc("skill_sandbox_failures_total")
            raise RuntimeError(
                "skill %s failed in sandbox: %s"
                % (self.name, error_output[-1000:].decode("utf-8", errors="replace"))
            )
        try:
            values = strict_json_loads(output)
            return _parse_skill_findings(values, parsed)
        except Exception as exc:
            metrics.inc("skill_output_rejections_total")
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
        if signing_key and len(signing_key.encode("utf-8")) < 32:
            raise ValueError("skill signing key must contain at least 32 bytes")
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
        if not isinstance(reviewer, Reviewer):
            raise TypeError("skill reviewer must implement Reviewer")
        if not valid_reviewer_name(reviewer.name):
            raise ValueError("skill reviewer must expose a valid name")
        _validate_skill_metadata(name, version, description)
        if (
            not isinstance(source, str)
            or not source
            or source != source.strip()
            or len(source) > MAX_SKILL_SOURCE_LABEL_CHARS
            or not source.isprintable()
        ):
            raise ValueError("invalid skill source label: %s" % name)
        with self._lock:
            if name in self._info:
                raise ValueError("duplicate registered skill name: %s" % name)
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
                if item.sandboxed:
                    value["source"] = "dynamic"
                else:
                    value.pop("protocol_version")
                value["permissions"] = list(value["permissions"])
                values.append(value)
            return values

    def revision(self) -> str:
        """Return a path-independent digest of the executable Skill set."""
        with self._lock:
            inventory = []
            for name in sorted(self._info):
                info = self._info[name]
                reviewer = self._skills[name]
                content_sha256 = str(getattr(reviewer, "source_sha256", ""))
                if not content_sha256:
                    prompt = getattr(reviewer, "system_prompt", "")
                    content_sha256 = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
                inventory.append(
                    {
                        "name": name,
                        "version": info.version,
                        "source": "dynamic" if info.sandboxed else info.source,
                        "sandboxed": info.sandboxed,
                        "permissions": list(info.permissions),
                        "protocol_version": info.protocol_version,
                        "reviewer": "%s.%s"
                        % (type(reviewer).__module__, type(reviewer).__qualname__),
                        "reviewer_name": reviewer.name,
                        "content_sha256": content_sha256,
                    }
                )
            runtime = (
                {
                    "sandbox": self.sandbox,
                    "timeout_seconds": self.timeout_seconds,
                    "memory_mb": self.memory_mb,
                    "container_image": next(
                        str(getattr(self._skills[name], "container_image", ""))
                        for name, item in self._info.items()
                        if item.sandboxed
                    ),
                    "require_container": self.require_container,
                }
                if any(item.sandboxed for item in self._info.values())
                else None
            )
        encoded = json.dumps(
            {"inventory": inventory, "runtime": runtime},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def reload(self) -> builtins.list[dict]:
        if not os.path.isdir(self.skills_dir):
            raise ValueError("skill directory is unavailable: %s" % self.skills_dir)
        root = os.path.realpath(self.skills_dir)
        candidates: dict[str, Reviewer] = {}
        candidate_info: dict[str, SkillInfo] = {}
        with os.scandir(self.skills_dir) as scan:
            entries = list(islice(scan, MAX_SKILL_ENTRIES + 1))
        if len(entries) > MAX_SKILL_ENTRIES:
            raise ValueError("skill directory exceeds the %d-entry limit" % MAX_SKILL_ENTRIES)
        for entry in sorted(entries, key=lambda item: item.name):
            if not entry.is_dir():
                continue
            if entry.is_symlink():
                raise ValueError("skill directories must not be symbolic links: %s" % entry.name)
            manifest_path = os.path.join(entry.path, "skill.json")
            if not os.path.isfile(manifest_path):
                continue
            if os.path.islink(manifest_path):
                raise ValueError("skill manifests must not be symbolic links: %s" % entry.name)
            with open(manifest_path, "rb") as handle:
                manifest_bytes = handle.read(MAX_SKILL_MANIFEST_BYTES + 1)
            if len(manifest_bytes) > MAX_SKILL_MANIFEST_BYTES:
                raise ValueError("skill manifest exceeds the 64 KiB limit: %s" % entry.name)
            manifest = strict_json_loads(manifest_bytes)
            if not isinstance(manifest, dict):
                raise ValueError("skill manifest must be a JSON object: %s" % entry.name)
            unknown = set(manifest).difference(
                {
                    "name",
                    "version",
                    "description",
                    "entrypoint",
                    "sha256",
                    "signature",
                    "permissions",
                    "protocol_version",
                }
            )
            if unknown:
                raise ValueError(
                    "unsupported skill manifest fields: %s" % ", ".join(sorted(unknown))
                )
            for field in (
                "name",
                "version",
                "entrypoint",
                "sha256",
                "permissions",
                "protocol_version",
            ):
                if field not in manifest:
                    raise ValueError("skill manifest is missing %s" % field)
            string_fields = ("name", "version", "entrypoint", "sha256")
            invalid_strings = [
                field for field in string_fields if not isinstance(manifest[field], str)
            ] + [
                field
                for field in ("description", "signature")
                if field in manifest and not isinstance(manifest[field], str)
            ]
            if invalid_strings:
                raise ValueError(
                    "skill manifest fields must be strings: %s" % ", ".join(sorted(invalid_strings))
                )
            name = str(manifest["name"])
            version = str(manifest["version"])
            description = str(manifest.get("description", "Dynamic review skill"))
            entrypoint = str(manifest["entrypoint"])
            _validate_skill_metadata(name, version, description)
            if not entrypoint or len(entrypoint) > MAX_SKILL_ENTRYPOINT_CHARS:
                raise ValueError("skill %s has invalid entrypoint" % name)
            skill_root = os.path.realpath(entry.path)
            if not skill_root.startswith(root + os.sep):
                raise ValueError("skill directory escapes the configured root: %s" % entry.name)
            module_path = os.path.realpath(os.path.join(skill_root, entrypoint))
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
                version,
                description,
                module_path,
                True,
                tuple(manifest.get("permissions", [])),
                SKILL_PROTOCOL_VERSION,
            )
        if candidates and self.require_container and not self.container_image:
            raise ValueError(
                "dynamic skills require EVOAGENT_SKILL_CONTAINER_IMAGE when "
                "EVOAGENT_SKILL_REQUIRE_CONTAINER=true"
            )
        if candidates and self.require_container and not self.signing_key:
            raise ValueError(
                "dynamic skills require EVOAGENT_SKILL_SIGNING_KEY when "
                "EVOAGENT_SKILL_REQUIRE_CONTAINER=true"
            )
        if candidates and self.container_image:
            image_id = resolve_container_image(self.container_image)
            for candidate in candidates.values():
                cast(SandboxedSkillReviewer, candidate).container_image = image_id
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
            identity_collisions = sorted(
                {reviewer.name for reviewer in retained_skills.values()}
                & {reviewer.name for reviewer in candidates.values()}
            )
            if identity_collisions:
                raise ValueError(
                    "dynamic reviewer names collide with trusted contributions: %s"
                    % ", ".join(identity_collisions)
                )
            retained_info = {name: info for name, info in self._info.items() if not info.sandboxed}
            self._skills = {**retained_skills, **candidates}
            self._info = {**retained_info, **candidate_info}

    def _validate_manifest(self, manifest: dict, module_path: str, source: bytes) -> None:
        name = str(manifest["name"])
        protocol_version = manifest["protocol_version"]
        if type(protocol_version) is not int or protocol_version != SKILL_PROTOCOL_VERSION:
            raise ValueError(
                "skill %s requires unsupported protocol version: %r" % (name, protocol_version)
            )
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
            expected = hmac.new(
                self.signing_key, _skill_signature_payload(manifest), hashlib.sha256
            ).hexdigest()
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
