"""Guard that the committed lock files stay consistent with pyproject.toml.

pyproject.toml is the single source of truth for direct dependencies; the lock
files are generated from it via pip-compile. These offline checks catch the
common drift modes without hitting the network:

* a dependency added to pyproject but not re-locked (missing from the lock);
* a dependency removed from pyproject but left behind in the lock;
* a pyproject version bound tightened so the pinned lock version now violates it.
"""

import re
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
_MARKER = "evoagent-pr-review"


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_lock(lock_path: Path) -> tuple[dict[str, str], set[str]]:
    """Return {canonical name: pinned version} and the set of direct (pyproject) names."""
    versions: dict[str, str] = {}
    direct: set[str] = set()
    current: str | None = None
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        pin = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", line)
        if pin:
            current = _canonical(pin.group(1))
            versions[current] = pin.group(2)
        elif current and _MARKER in line and "pyproject.toml" in line:
            direct.add(current)
    return versions, direct


class DependencyLockConsistencyTests(unittest.TestCase):
    def setUp(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            self.pyproject = tomllib.load(handle)
        self.runtime = [Requirement(dep) for dep in self.pyproject["project"]["dependencies"]]
        self.dev = [
            Requirement(dep) for dep in self.pyproject["project"]["optional-dependencies"]["dev"]
        ]

    def _assert_locked(self, requirements, lock_name, expected_direct):
        versions, direct = _parse_lock(ROOT / lock_name)
        for requirement in requirements:
            name = _canonical(requirement.name)
            self.assertIn(name, versions, "%s missing from %s" % (name, lock_name))
            locked = versions[name]
            self.assertTrue(
                requirement.specifier.contains(Version(locked), prereleases=True),
                "%s==%s in %s violates pyproject specifier '%s'"
                % (name, locked, lock_name, requirement.specifier),
            )
        self.assertEqual(
            expected_direct,
            direct,
            "direct deps in %s drifted from pyproject: %s"
            % (lock_name, expected_direct.symmetric_difference(direct)),
        )

    def test_runtime_lock_matches_pyproject(self):
        expected = {_canonical(r.name) for r in self.runtime}
        self._assert_locked(self.runtime, "requirements.lock", expected)

    def test_dev_lock_matches_pyproject(self):
        expected = {_canonical(r.name) for r in self.runtime + self.dev}
        self._assert_locked(self.runtime + self.dev, "requirements-dev.lock", expected)

    def test_no_orphan_requirements_txt(self):
        # pyproject + generated locks are the only dependency sources of truth.
        self.assertFalse(
            (ROOT / "requirements.txt").exists(),
            "requirements.txt duplicates pyproject dependencies; remove it to avoid drift",
        )

    def test_workflow_actions_are_pinned_to_full_commit_shas(self):
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for action, revision in re.findall(
                r"uses:\s+([^@\s]+)@([^\s#]+)", workflow.read_text(encoding="utf-8")
            ):
                self.assertRegex(
                    revision,
                    r"^[0-9a-f]{40}$",
                    "%s uses mutable action reference %s@%s" % (workflow.name, action, revision),
                )

    def test_workflow_service_images_are_pinned_by_digest(self):
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for image in re.findall(
                r"^\s+image:\s+(\S+)",
                workflow.read_text(encoding="utf-8"),
                re.MULTILINE,
            ):
                self.assertRegex(
                    image,
                    r"@sha256:[0-9a-f]{64}$",
                    "%s uses mutable service image %s" % (workflow.name, image),
                )

    def test_workflow_namespaced_cli_images_are_pinned_by_digest(self):
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for image in re.findall(
                r"\b([a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*:"
                r"[A-Za-z0-9._-]+(?:@sha256:[0-9a-f]{64})?)",
                workflow.read_text(encoding="utf-8"),
            ):
                self.assertRegex(
                    image,
                    r"@sha256:[0-9a-f]{64}$",
                    "%s uses mutable command image %s" % (workflow.name, image),
                )

    def test_docker_base_images_are_pinned_by_digest(self):
        for dockerfile in ROOT.glob("Dockerfile*"):
            stages = set()
            for image, alias in re.findall(
                r"^FROM(?:[ \t]+--platform=\S+)?[ \t]+(\S+)(?:[ \t]+AS[ \t]+(\S+))?",
                dockerfile.read_text(encoding="utf-8"),
                re.MULTILINE | re.IGNORECASE,
            ):
                self.assertTrue(
                    image == "scratch"
                    or image.lower() in stages
                    or re.search(r"@sha256:[0-9a-f]{64}$", image),
                    "%s uses mutable base image %s" % (dockerfile.name, image),
                )
                if alias:
                    stages.add(alias.lower())

    def test_docker_python_version_matches_supported_runtime(self):
        versions = re.findall(
            r"^FROM python:([0-9.]+)-", (ROOT / "Dockerfile").read_text(), re.MULTILINE
        )
        self.assertTrue(versions, "Dockerfile must declare its Python version")
        supported = SpecifierSet(self.pyproject["project"]["requires-python"])
        for version in versions:
            self.assertIn(Version(version), supported, "Docker Python is outside requires-python")

    def test_compose_images_are_pinned_by_digest(self):
        for compose_file in ROOT.glob("*compose*.yml"):
            for image in re.findall(
                r"^\s+image:\s+(\S+)",
                compose_file.read_text(encoding="utf-8"),
                re.MULTILINE,
            ):
                self.assertRegex(
                    image,
                    r"@sha256:[0-9a-f]{64}$",
                    "%s uses mutable image %s" % (compose_file.name, image),
                )

    def test_perf_port_can_move_without_exposing_the_fixture(self):
        compose = (ROOT / "docker-compose.perf.yml").read_text()
        self.assertIn('"127.0.0.1:${EVOAGENT_PERF_PORT:-8080}:8080"', compose)

    def test_proof_executor_declares_daemon_restart_policy(self):
        compose = (ROOT / "docker-compose.proof.yml").read_text()
        executor = compose.split("  proof-executor:\n", 1)[1].split("\n  evoagent:", 1)[0]
        self.assertIn("\n    restart: always\n", executor)

    def test_durable_compose_services_restart_after_daemon_or_vm_recovery(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        for service, following in (
            ("postgres", "redis"),
            ("redis", "migrate"),
            ("evoagent", None),
        ):
            block = compose.split("  %s:\n" % service, 1)[1]
            if following:
                block = block.split("\n  %s:\n" % following, 1)[0]
            self.assertIn("\n    restart: always\n", block, service)

    def test_local_env_variants_never_enter_git_or_docker_context(self):
        for name in (".gitignore", ".dockerignore"):
            lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
            self.assertLess(lines.index(".env*"), lines.index("!.env.example"), name)


if __name__ == "__main__":
    unittest.main()
