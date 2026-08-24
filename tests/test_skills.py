import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from evoagent import skill_runner
from evoagent.diff_parser import parse_unified_diff
from evoagent.metrics import Metrics
from evoagent.models import Finding, Severity
from evoagent.reviewer import MAX_REVIEWER_NAME_CHARS, Reviewer
from evoagent.skill_signing import sign_manifest
from evoagent.skills import (
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_ENTRYPOINT_CHARS,
    MAX_SKILL_MANIFEST_BYTES,
    MAX_SKILL_VERSION_CHARS,
    SandboxedSkillReviewer,
    SkillRegistry,
    _run_bounded,
)

DIFF = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+value = 1\n"
SOURCE = """\
from evoagent.reviewer import Reviewer

class SnapshotReviewer(Reviewer):
    name = "snapshot-reviewer"

    def review(self, diff, parsed):
        return []

def create_skill():
    return SnapshotReviewer()
"""


class EmptyReviewer(Reviewer):
    name = "empty-reviewer"

    def review(self, _diff, _parsed):
        return []


def _skill_finding(**overrides):
    fields = {
        "rule_id": "TEST",
        "severity": Severity.LOW,
        "title": "Actionable issue",
        "explanation": "The added value needs review.",
        "path": "app.py",
        "line": 1,
        "evidence": "value = 1",
        "fix": "Use a validated value.",
        "test": "Cover the invalid value.",
        "confidence": 0.8,
    }
    fields.update(overrides)
    return Finding(**fields).to_dict()


class SkillRegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.skills_dir = Path(self.temporary.name) / "skills"
        self.skills_dir.mkdir()

    def test_sandbox_resource_limits_cannot_be_disabled(self):
        for name, value in (
            ("timeout_seconds", 0),
            ("memory_mb", 0),
            ("timeout_seconds", float("nan")),
            ("memory_mb", True),
        ):
            with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, "limits"):
                SandboxedSkillReviewer("test", "skill.py", **{name: value})

    def write_skill(
        self,
        directory: str,
        name: str,
        source: str = SOURCE,
        checksum: str | None = None,
    ) -> Path:
        root = self.skills_dir / directory
        root.mkdir()
        module = root / "skill.py"
        module.write_text(source, encoding="utf-8")
        manifest = {
            "name": name,
            "version": "1.0.0",
            "protocol_version": 1,
            "description": "test skill",
            "entrypoint": "skill.py",
            "sha256": checksum or hashlib.sha256(source.encode()).hexdigest(),
            "permissions": [],
        }
        (root / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_missing_skill_directory_fails_closed_without_creating_it(self):
        missing = Path(self.temporary.name) / "missing"

        with self.assertRaisesRegex(ValueError, "skill directory is unavailable"):
            SkillRegistry(str(missing)).reload()

        self.assertFalse(missing.exists())

    def test_execution_uses_the_verified_snapshot_not_a_mutated_source_path(self):
        root = self.write_skill("snapshot", "snapshot")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        reviewer = registry.reviewers()[0]
        self.assertEqual("dynamic", registry.list()[0]["source"])
        self.assertEqual(1, registry.list()[0]["protocol_version"])
        self.assertNotIn(str(root), json.dumps(registry.list()))
        (root / "skill.py").write_text("raise RuntimeError('tampered')\n", encoding="utf-8")

        findings = reviewer.review(DIFF, parse_unified_diff(DIFF))

        self.assertEqual([], findings)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            registry.reload()
        self.assertIs(reviewer, registry.reviewers()[0])

    def test_failed_reload_keeps_the_previous_dynamic_set(self):
        self.write_skill("stable", "stable")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        self.write_skill("broken", "broken", checksum="0" * 64)

        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            registry.reload()

        self.assertEqual(["stable"], [item["name"] for item in registry.list()])

    def test_manifest_is_bounded_before_json_parsing(self):
        root = self.write_skill("oversized", "oversized")
        (root / "skill.json").write_bytes(b" " * (MAX_SKILL_MANIFEST_BYTES + 1))

        with self.assertRaisesRegex(ValueError, "manifest exceeds the 64 KiB limit"):
            SkillRegistry(str(self.skills_dir)).reload()

    def test_manifest_metadata_is_bounded_before_path_access(self):
        root = self.write_skill("metadata", "metadata")
        manifest_path = root / "skill.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases = {
            "name": "x" * (MAX_REVIEWER_NAME_CHARS + 1),
            "version": "1" * (MAX_SKILL_VERSION_CHARS + 1),
            "description": "x" * (MAX_SKILL_DESCRIPTION_CHARS + 1),
            "entrypoint": "x" * (MAX_SKILL_ENTRYPOINT_CHARS + 1),
        }

        for field, value in cases.items():
            with self.subTest(field=field):
                manifest_path.write_text(json.dumps({**original, field: value}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, field):
                    SkillRegistry(str(self.skills_dir)).reload()

    def test_trusted_registration_uses_the_same_metadata_bounds(self):
        registry = SkillRegistry(str(self.skills_dir))

        with self.assertRaisesRegex(ValueError, "description"):
            registry.register(
                "trusted",
                EmptyReviewer(),
                description="x" * (MAX_SKILL_DESCRIPTION_CHARS + 1),
            )

        self.assertEqual([], registry.list())

    def test_trusted_registration_rejects_invalid_reviewers_immediately(self):
        registry = SkillRegistry(str(self.skills_dir))

        with self.assertRaisesRegex(TypeError, "Reviewer"):
            registry.register("trusted", object())
        for name in (None, "", "two words", "x" * (MAX_REVIEWER_NAME_CHARS + 1)):
            reviewer = EmptyReviewer()
            reviewer.name = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "valid name"):
                registry.register("trusted", reviewer)

        self.assertEqual([], registry.list())

    def test_trusted_registration_cannot_replace_an_existing_name(self):
        registry = SkillRegistry(str(self.skills_dir))
        original = EmptyReviewer()
        registry.register("trusted", original, source="company.reviewer")

        with self.assertRaisesRegex(ValueError, "duplicate registered skill"):
            registry.register("trusted", EmptyReviewer(), source="company.reviewer")

        self.assertEqual([original], registry.reviewers())

    def test_skill_directory_entry_count_is_bounded_before_loading(self):
        for name in ("one", "two", "three"):
            (self.skills_dir / name).mkdir()

        with (
            mock.patch("evoagent.skills.MAX_SKILL_ENTRIES", 2),
            self.assertRaisesRegex(ValueError, "2-entry limit"),
        ):
            SkillRegistry(str(self.skills_dir)).reload()

    def test_manifest_rejects_unknown_fields_before_activation(self):
        root = self.write_skill("unknown", "unknown")
        manifest_path = root / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["permisisons"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        registry = SkillRegistry(str(self.skills_dir))

        with self.assertRaisesRegex(ValueError, "unsupported.*permisisons"):
            registry.reload()

        self.assertEqual([], registry.list())

    def test_manifest_rejects_duplicate_fields_without_replacing_the_active_set(self):
        self.write_skill("stable", "stable")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        root = self.write_skill("duplicate", "duplicate")
        manifest_path = root / "skill.json"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                '"name": "duplicate"', '"name": "visible", "name": "effective"'
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "duplicate field: name"):
            registry.reload()

        self.assertEqual(["stable"], [item["name"] for item in registry.list()])

    def test_signature_binds_the_manifest_and_requires_a_strong_key(self):
        with self.assertRaisesRegex(ValueError, "32 bytes"):
            SkillRegistry(str(self.skills_dir), signing_key="short")

        root = self.write_skill("signed", "signed")
        manifest_path = root / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        key = "k" * 32
        payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        manifest["signature"] = hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        registry = SkillRegistry(str(self.skills_dir), signing_key=key)
        registry.reload()

        manifest["version"] = "1.0.1"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "signature mismatch"):
            registry.reload()

        self.assertEqual("1.0.0", registry.list()[0]["version"])

    def test_signer_updates_source_hash_and_emits_a_loadable_manifest(self):
        root = self.write_skill("release", "release")
        manifest_path = root / "skill.json"
        key = "release-key-" * 3
        changed = SOURCE.replace("return []", "return []  # release")
        (root / "skill.py").write_text(changed, encoding="utf-8")

        signature = sign_manifest(str(manifest_path), key)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(changed.encode()).hexdigest(), manifest["sha256"])
        self.assertEqual(signature, manifest["signature"])
        self.assertEqual(
            ["release"],
            [
                item["name"]
                for item in SkillRegistry(str(self.skills_dir), signing_key=key).reload()
            ],
        )

    def test_manifest_does_not_coerce_a_boolean_skill_name(self):
        root = self.write_skill("boolean", "boolean")
        manifest_path = root / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        registry = SkillRegistry(str(self.skills_dir))

        with self.assertRaisesRegex(ValueError, "fields must be strings: name"):
            registry.reload()

        self.assertEqual([], registry.list())

    def test_manifest_requires_the_supported_protocol_version(self):
        for index, value in enumerate((None, True, "1", 2)):
            with self.subTest(value=value):
                root = self.write_skill("protocol-%d" % index, "protocol-%d" % index)
                manifest_path = root / "skill.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if value is None:
                    manifest.pop("protocol_version")
                else:
                    manifest["protocol_version"] = value
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                registry = SkillRegistry(str(self.skills_dir))

                with self.assertRaisesRegex(ValueError, "protocol_version|protocol version"):
                    registry.reload()

                self.assertEqual([], registry.list())
                for child in root.iterdir():
                    child.unlink()
                root.rmdir()

    def test_successful_reload_retires_removed_dynamic_skills(self):
        root = self.write_skill("retired", "retired")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        os.rename(root, Path(self.temporary.name) / "retired")

        registry.reload()

        self.assertEqual([], registry.list())

    def test_revision_changes_only_when_the_verified_skill_set_changes(self):
        root = self.write_skill("revision", "revision")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        original = registry.revision()
        changed = SOURCE.replace("return []", "return []  # changed")
        (root / "skill.py").write_text(changed, encoding="utf-8")
        manifest_path = root / "skill.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sha256"] = hashlib.sha256(changed.encode()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        registry.reload()
        changed_revision = registry.revision()
        registry.reload()

        self.assertNotEqual(original, changed_revision)
        self.assertEqual(changed_revision, registry.revision())

    def test_revision_binds_dynamic_skill_runtime(self):
        self.write_skill("runtime", "runtime")
        original = SkillRegistry(str(self.skills_dir), timeout_seconds=30)
        changed = SkillRegistry(str(self.skills_dir), timeout_seconds=31)
        original.reload()
        changed.reload()

        self.assertNotEqual(original.revision(), changed.revision())

    def test_dynamic_skill_cannot_shadow_a_trusted_contribution(self):
        self.write_skill("collision", "trusted-review")
        registry = SkillRegistry(str(self.skills_dir))
        trusted = EmptyReviewer()
        registry.register("trusted-review", trusted, source="company.reviewer")

        with self.assertRaisesRegex(ValueError, "collide with trusted"):
            registry.reload()

        self.assertEqual([trusted], registry.reviewers())

    def test_dynamic_reviewer_identity_cannot_shadow_a_trusted_contribution(self):
        self.write_skill("collision", "empty-reviewer")
        registry = SkillRegistry(str(self.skills_dir))
        trusted = EmptyReviewer()
        registry.register("trusted-review", trusted, source="company.reviewer")

        with self.assertRaisesRegex(ValueError, "reviewer names collide with trusted"):
            registry.reload()

        self.assertEqual([trusted], registry.reviewers())

    def test_container_required_mode_fails_closed_before_activation(self):
        self.write_skill("container-required", "container-required")
        registry = SkillRegistry(str(self.skills_dir), require_container=True)

        with self.assertRaisesRegex(ValueError, "SKILL_CONTAINER_IMAGE"):
            registry.reload()

        self.assertEqual([], registry.list())

    def test_container_mode_requires_a_preloaded_reachable_image(self):
        root = self.write_skill("container", "container")
        key = "container-signing-key-" * 2
        sign_manifest(str(root / "skill.json"), key)
        registry = SkillRegistry(
            str(self.skills_dir),
            signing_key=key,
            container_image="company/reviewer@sha256:" + "a" * 64,
            require_container=True,
        )

        with mock.patch("evoagent.skills.subprocess.run") as inspect:
            inspect.return_value.returncode = 1
            inspect.return_value.stdout = ""
            with self.assertRaisesRegex(ValueError, "daemon or image is unavailable"):
                registry.reload()
            self.assertEqual([], registry.list())
            inspect.reset_mock()
            inspect.return_value.returncode = 0
            inspect.return_value.stdout = "sha256:" + "b" * 64
            self.assertEqual(["container"], [item["name"] for item in registry.reload()])
            inspect.assert_called_once_with(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    "company/reviewer@sha256:" + "a" * 64,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                text=True,
                check=False,
            )
            self.assertEqual(
                "sha256:" + "b" * 64,
                registry.reviewers()[0].container_image,
            )
            revision = registry.revision()
            inspect.reset_mock()
            inspect.return_value.returncode = 0
            inspect.return_value.stdout = "sha256:" + "c" * 64
            registry.reload()
            self.assertNotEqual(revision, registry.revision())

    def test_container_required_mode_rejects_unsigned_dynamic_skills(self):
        self.write_skill("unsigned", "unsigned")
        registry = SkillRegistry(
            str(self.skills_dir),
            container_image="company/reviewer@sha256:" + "a" * 64,
            require_container=True,
        )

        with (
            mock.patch("evoagent.skills.subprocess.run") as inspect,
            self.assertRaisesRegex(ValueError, "SKILL_SIGNING_KEY"),
        ):
            registry.reload()

        inspect.assert_not_called()
        self.assertEqual([], registry.list())

    def test_runner_rejects_a_mismatched_snapshot_digest(self):
        reviewer = SandboxedSkillReviewer(
            "invalid-snapshot",
            str(self.skills_dir / "invalid.py"),
            source=SOURCE,
            source_sha256="0" * 64,
        )

        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            reviewer.review(DIFF, parse_unified_diff(DIFF))

    def test_runner_rejects_an_unsupported_protocol_before_loading_source(self):
        for value in (None, True, "1", 2):
            with (
                self.subTest(value=value),
                mock.patch.object(sys, "argv", ["skill_runner.py", "skill.py"]),
                mock.patch(
                    "evoagent.skill_runner.json.load", return_value={"protocol_version": value}
                ),
                self.assertRaisesRegex(RuntimeError, "unsupported skill protocol"),
            ):
                skill_runner.main()

    def test_runner_uses_the_configured_resource_limits(self):
        source = "class Skill:\n    def review(self, diff, parsed): return []\ndef create_skill(): return Skill()\n"
        payload = {
            "protocol_version": 1,
            "skill_source": source,
            "skill_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "memory_mb": 128,
            "timeout_seconds": 47,
            "parsed": {"files": [], "added_lines": []},
            "diff": "",
        }
        for field in ("memory_mb", "timeout_seconds"):
            with (
                self.subTest(field=field),
                mock.patch.object(sys, "argv", ["skill_runner.py", "skill.py"]),
                mock.patch("evoagent.skill_runner.json.load", return_value={**payload, field: 0}),
                self.assertRaisesRegex(RuntimeError, "resource limits"),
            ):
                skill_runner.main()
        with (
            mock.patch.object(sys, "argv", ["skill_runner.py", "skill.py"]),
            mock.patch("evoagent.skill_runner.json.load", return_value=payload),
            mock.patch("evoagent.skill_runner.json.dump"),
            mock.patch("evoagent.skill_runner.sys.addaudithook"),
            mock.patch("evoagent.skill_runner._set_resource_limit") as set_limit,
        ):
            skill_runner.main()

        set_limit.assert_any_call(mock.ANY, "RLIMIT_AS", 128 * 1024 * 1024)
        set_limit.assert_any_call(mock.ANY, "RLIMIT_CPU", 47)

    def test_runner_cannot_read_the_application_root(self):
        application_root = Path(__file__).resolve().parents[1]
        protected_file = application_root / "pyproject.toml"
        source = SOURCE.replace(
            "return []",
            "try:\n"
            f"            open({str(protected_file)!r}, encoding='utf-8').close()\n"
            "        except PermissionError:\n"
            "            return []\n"
            "        raise RuntimeError('application root exposed')",
        )
        reviewer = SandboxedSkillReviewer(
            "root-probe",
            str(self.skills_dir / "probe.py"),
            source=source,
            source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        )

        self.assertEqual([], reviewer.review(DIFF, parse_unified_diff(DIFF)))

    def test_runner_stops_an_unbounded_stdout_stream_before_timeout(self):
        noisy_source = SOURCE.replace("return []", 'while True:\n            print("x" * 65536)')
        reviewer = SandboxedSkillReviewer(
            "noisy-snapshot",
            str(self.skills_dir / "noisy.py"),
            timeout_seconds=5,
            source=noisy_source,
            source_sha256=hashlib.sha256(noisy_source.encode()).hexdigest(),
        )

        with self.assertRaisesRegex(RuntimeError, "output limit"):
            reviewer.review(DIFF, parse_unified_diff(DIFF))

    @unittest.skipIf(os.name == "nt", "Windows has no stdlib process-tree kill")
    def test_bounded_runner_kills_descendants_holding_output_pipes(self):
        command = [
            sys.executable,
            "-c",
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "time.sleep(30)",
        ]
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            returncode, _output, _error, timed_out, _exceeded = _run_bounded(
                command, b"", directory, dict(os.environ), 1
            )

        self.assertTrue(timed_out)
        self.assertNotEqual(0, returncode)
        self.assertLess(time.monotonic() - started, 5)

    def test_runner_rejects_an_excessive_finding_set(self):
        reviewer = SandboxedSkillReviewer(
            "noisy-findings",
            str(self.skills_dir / "noisy.py"),
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        finding = {
            "rule_id": "TEST",
            "severity": "low",
            "title": "title",
            "explanation": "explanation",
            "path": "app.py",
            "line": 1,
            "evidence": "value = 1",
            "fix": "fix",
            "test": "test",
            "confidence": 0.8,
        }

        with (
            mock.patch(
                "evoagent.skills._run_bounded",
                return_value=(0, json.dumps([finding] * 101).encode(), b"", False, False),
            ),
            self.assertRaisesRegex(RuntimeError, "invalid output"),
        ):
            reviewer.review(DIFF, parse_unified_diff(DIFF))

    def test_parent_accepts_one_exact_v1_finding(self):
        reviewer = SandboxedSkillReviewer(
            "valid-output",
            str(self.skills_dir / "valid.py"),
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        payload = _skill_finding()

        with mock.patch(
            "evoagent.skills._run_bounded",
            return_value=(0, json.dumps([payload]).encode(), b"", False, False),
        ):
            findings = reviewer.review(DIFF, parse_unified_diff(DIFF))

        self.assertEqual([Finding.from_dict(payload)], findings)

    def test_metrics_distinguish_success_and_bounded_failure_modes(self):
        reviewer = SandboxedSkillReviewer(
            "observable-output",
            str(self.skills_dir / "observable.py"),
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        parsed = parse_unified_diff(DIFF)
        captured = Metrics()
        with (
            mock.patch("evoagent.skills.metrics", captured),
            mock.patch(
                "evoagent.skills._run_bounded",
                return_value=(
                    0,
                    json.dumps([_skill_finding()]).encode(),
                    b"",
                    False,
                    False,
                ),
            ),
        ):
            reviewer.review(DIFF, parsed)
        output = captured.prometheus()
        self.assertIn("evoagent_skill_runs_total 1.0", output)
        self.assertIn("evoagent_skill_successes_total 1.0", output)
        self.assertIn("evoagent_skill_findings_total 1.0", output)
        self.assertIn("evoagent_skill_execution_count 1", output)

        failures = (
            ((1, b"", b"", True, False), "skill_timeouts_total"),
            ((1, b"", b"", False, True), "skill_output_limit_rejections_total"),
            ((1, b"", b"sandbox failed", False, False), "skill_sandbox_failures_total"),
            ((0, b"{}", b"", False, False), "skill_output_rejections_total"),
        )
        for result, counter in failures:
            captured = Metrics()
            with (
                self.subTest(counter=counter),
                mock.patch("evoagent.skills.metrics", captured),
                mock.patch("evoagent.skills._run_bounded", return_value=result),
                self.assertRaises(RuntimeError),
            ):
                reviewer.review(DIFF, parsed)
            output = captured.prometheus()
            self.assertIn("evoagent_skill_runs_total 1.0", output)
            self.assertIn("evoagent_skill_failures_total 1.0", output)
            self.assertIn("evoagent_%s 1.0" % counter, output)
            self.assertIn("evoagent_skill_execution_count 1", output)

    def test_parent_rejects_noncanonical_or_unanchored_v1_findings(self):
        reviewer = SandboxedSkillReviewer(
            "invalid-output",
            str(self.skills_dir / "invalid.py"),
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        valid = _skill_finding()
        missing = dict(valid)
        missing.pop("confidence")
        invalid_severity = {**valid, "severity": "LOW"}
        cases = (
            {**valid, "future_field": True},
            missing,
            invalid_severity,
            {**valid, "fingerprint": "0" * 16},
            _skill_finding(path="other.py"),
            _skill_finding(line=2),
            _skill_finding(evidence="not on the added line"),
            _skill_finding(title="x" * 201),
        )

        for payload in cases:
            with (
                self.subTest(payload=payload),
                mock.patch(
                    "evoagent.skills._run_bounded",
                    return_value=(0, json.dumps([payload]).encode(), b"", False, False),
                ),
                self.assertRaisesRegex(RuntimeError, "invalid output"),
            ):
                reviewer.review(DIFF, parse_unified_diff(DIFF))

    def test_runner_rejects_ambiguous_or_nonstandard_json_output(self):
        reviewer = SandboxedSkillReviewer(
            "invalid-json",
            str(self.skills_dir / "invalid.py"),
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        finding = {
            "rule_id": "TEST",
            "severity": "low",
            "title": "title",
            "explanation": "explanation",
            "path": "app.py",
            "line": 1,
            "evidence": "value = 1",
            "fix": "fix",
            "test": "test",
            "confidence": 0.8,
        }
        encoded = json.dumps(finding)
        outputs = (
            encoded.replace('"title": "title"', '"title": "first", "title": "title"'),
            json.dumps({**finding, "ignored": float("nan")}),
            json.dumps({**finding, "title": "\ud800"}),
        )

        for output in outputs:
            with (
                self.subTest(output=output),
                mock.patch(
                    "evoagent.skills._run_bounded",
                    return_value=(0, ("[" + output + "]").encode(), b"", False, False),
                ),
                self.assertRaisesRegex(RuntimeError, "invalid output"),
            ):
                reviewer.review(DIFF, parse_unified_diff(DIFF))

    def test_container_uses_immutable_runtime_image_without_host_mounts(self):
        reviewer = SandboxedSkillReviewer(
            "container-snapshot",
            str(self.skills_dir / "skill.py"),
            container_image="sha256:" + "a" * 64,
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        command = []

        def run(value, *_args, **_kwargs):
            command.extend(value)
            return 0, b"[]", b"", False, False

        with (
            mock.patch("evoagent.skills._run_bounded", side_effect=run),
            mock.patch(
                "evoagent.skills.subprocess.run", return_value=mock.Mock(returncode=0)
            ) as rm,
        ):
            reviewer.review(DIFF, parse_unified_diff(DIFF))

        self.assertEqual("never", command[command.index("--pull") + 1])
        self.assertIn("--name", command)
        self.assertNotIn("-v", command)
        if os.name != "nt":
            self.assertEqual("65534:65534", command[command.index("--user") + 1])
        self.assertIn("sha256:" + "a" * 64, command)
        self.assertEqual(["docker", "rm", "-f"], rm.call_args.args[0][:3])

    def test_container_cleanup_failure_fails_closed_and_is_observable(self):
        reviewer = SandboxedSkillReviewer(
            "container-cleanup",
            str(self.skills_dir / "skill.py"),
            container_image="sha256:" + "a" * 64,
            source=SOURCE,
            source_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
        captured = Metrics()

        with (
            mock.patch("evoagent.skills.metrics", captured),
            mock.patch("evoagent.skills._run_bounded", return_value=(0, b"[]", b"", False, False)),
            mock.patch("evoagent.skills.subprocess.run", return_value=mock.Mock(returncode=1)),
            self.assertRaisesRegex(RuntimeError, "container cleanup failed"),
        ):
            reviewer.review(DIFF, parse_unified_diff(DIFF))

        self.assertIn("evoagent_skill_container_cleanup_failures_total 1.0", captured.prometheus())


if __name__ == "__main__":
    unittest.main()
