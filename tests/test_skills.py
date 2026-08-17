import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from evoagent.diff_parser import parse_unified_diff
from evoagent.reviewer import Reviewer
from evoagent.skills import SandboxedSkillReviewer, SkillRegistry

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


class SkillRegistryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.skills_dir = Path(self.temporary.name) / "skills"
        self.skills_dir.mkdir()

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
            "description": "test skill",
            "entrypoint": "skill.py",
            "sha256": checksum or hashlib.sha256(source.encode()).hexdigest(),
            "permissions": [],
        }
        (root / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def test_execution_uses_the_verified_snapshot_not_a_mutated_source_path(self):
        root = self.write_skill("snapshot", "snapshot")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        reviewer = registry.reviewers()[0]
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

    def test_successful_reload_retires_removed_dynamic_skills(self):
        root = self.write_skill("retired", "retired")
        registry = SkillRegistry(str(self.skills_dir))
        registry.reload()
        os.rename(root, Path(self.temporary.name) / "retired")

        registry.reload()

        self.assertEqual([], registry.list())

    def test_dynamic_skill_cannot_shadow_a_trusted_contribution(self):
        self.write_skill("collision", "trusted-review")
        registry = SkillRegistry(str(self.skills_dir))
        trusted = EmptyReviewer()
        registry.register("trusted-review", trusted, source="company.reviewer")

        with self.assertRaisesRegex(ValueError, "collide with trusted"):
            registry.reload()

        self.assertEqual([trusted], registry.reviewers())

    def test_container_required_mode_fails_closed_before_activation(self):
        self.write_skill("container-required", "container-required")
        registry = SkillRegistry(str(self.skills_dir), require_container=True)

        with self.assertRaisesRegex(ValueError, "SKILL_CONTAINER_IMAGE"):
            registry.reload()

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

    def test_runner_caps_untrusted_stdout_before_loading_it_into_memory(self):
        noisy_source = SOURCE.replace("return []", 'print("x" * 2_000_000)\n        return []')
        reviewer = SandboxedSkillReviewer(
            "noisy-snapshot",
            str(self.skills_dir / "noisy.py"),
            source=noisy_source,
            source_sha256=hashlib.sha256(noisy_source.encode()).hexdigest(),
        )

        with self.assertRaisesRegex(RuntimeError, "output limit"):
            reviewer.review(DIFF, parse_unified_diff(DIFF))


if __name__ == "__main__":
    unittest.main()
