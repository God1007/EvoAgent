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


if __name__ == "__main__":
    unittest.main()
