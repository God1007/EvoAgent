"""Proof Runner: turn a review claim into executable evidence (MVP).

An LLM saying "this is a bug" is an opinion; a failing reproduction that a patch
turns green is proof. This module grades a claim on an **evidence ladder** by
actually running a reproduction (and optionally the regression suite) inside the
sandboxed :class:`~evoagent.verifier.RepairVerifier`:

* **L1 – static**: only a static/heuristic signal (or no runnable reproduction).
* **L2 – reproduced**: the reproduction *fails on the original code* — the issue
  is real and demonstrable.
* **L3 – fix verified**: after applying the patch the same reproduction *passes*
  — the fix provably resolves the reproduced issue.
* **L4 – regression clean**: the patch is L3 *and* the regression suite still
  passes — the fix does not break anything measured.

It also returns the before/after command output and the unified patch diff so a
human (or a downstream gate) can audit exactly what was proven.

The reproduction contract is the classic before/after invariant: the command
must **fail on the original** and **pass on the patched** sources. Execution
isolation is entirely delegated to the injected verifier; run it with a
container image for untrusted code.
"""

import difflib
import os
import shutil
import tempfile
from enum import IntEnum
from typing import Any

from .errors import ClientInputError, safe_exception_summary
from .ports import ProofExecutorPort
from .verifier import RepairVerifier


class EvidenceLevel(IntEnum):
    L1_STATIC = 1
    L2_REPRODUCED = 2
    L3_FIX_VERIFIED = 3
    L4_REGRESSION_CLEAN = 4


LEVEL_LABELS = {
    EvidenceLevel.L1_STATIC: "L1-static",
    EvidenceLevel.L2_REPRODUCED: "L2-reproduced",
    EvidenceLevel.L3_FIX_VERIFIED: "L3-fix-verified",
    EvidenceLevel.L4_REGRESSION_CLEAN: "L4-regression-clean",
}
MAX_PROOF_FILES = 5_000


def _diff_lines(text: str) -> list[str]:
    # Guarantee every line ends in "\n" so a file without a trailing newline does
    # not cause difflib's next control line to merge onto the content line.
    return [line if line.endswith("\n") else line + "\n" for line in text.splitlines(keepends=True)]


def unified_patch(original: dict[str, str], patched: dict[str, str]) -> str:
    """Unified diff across the union of files (added, removed and modified)."""
    chunks: list[str] = []
    for path in sorted(set(original) | set(patched)):
        before = _diff_lines(original.get(path, ""))
        after = _diff_lines(patched.get(path, ""))
        if before == after:
            continue
        diff = difflib.unified_diff(before, after, fromfile="a/%s" % path, tofile="b/%s" % path)
        chunks.append("".join(diff))
    return "".join(chunks)


class LocalProofExecutor:
    """Materialize and execute locally through an injected isolation adapter."""

    def __init__(self, verifier_factory=None):
        self._verifier_factory = verifier_factory or (
            lambda command: RepairVerifier(test_command=command)
        )

    def execute(self, files: dict[str, str], command: str) -> dict[str, Any]:
        root = tempfile.mkdtemp(prefix="evoagent-proof-")
        try:
            _materialize(files, root)
            verifier = self._verifier_factory(command)
            return verifier.verify_worktree(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class ProofRunner:
    def __init__(self, verifier_factory=None, executor: ProofExecutorPort | None = None):
        """``verifier_factory(command) -> RepairVerifier`` builds a verifier bound
        to a specific command, so the runner inherits the operator's isolation
        configuration (container image, limits) for every step."""
        if verifier_factory is not None and executor is not None:
            raise ValueError("configure either verifier_factory or executor, not both")
        self.executor = executor or LocalProofExecutor(verifier_factory)

    def prove(
        self,
        original_files: dict[str, str],
        patched_files: dict[str, str],
        reproduction_command: str = "",
        regression_command: str = "",
    ) -> dict:
        paths = set(original_files) | set(patched_files)
        if len(paths) > MAX_PROOF_FILES:
            raise ClientInputError("proof file set cannot contain more than 5000 distinct paths")
        for path in paths:
            _proof_target(os.curdir, path)
        patch = unified_patch(original_files, patched_files)
        result: dict = {
            "evidence_level": int(EvidenceLevel.L1_STATIC),
            "evidence_label": LEVEL_LABELS[EvidenceLevel.L1_STATIC],
            "patch": patch,
            "steps": [],
        }
        reproduction_command = reproduction_command.strip()
        regression_command = regression_command.strip()
        if not reproduction_command:
            result["note"] = (
                "No runnable reproduction was provided; the claim rests on static "
                "analysis only (L1)."
            )
            return result

        original = self._run(original_files, reproduction_command)
        result["steps"].append({"step": "reproduce-on-original", **original})
        if original["status"] != "failed":
            # Only a genuine non-zero test result on the original code proves the
            # bug. "passed" means nothing reproduced; "timeout"/"error" means we
            # could not run it — neither may be sold as evidence.
            if original["status"] == "passed":
                result["note"] = (
                    "Reproduction did not fail on the original code; the issue is "
                    "unproven (still L1)."
                )
            else:
                result["note"] = (
                    "Could not run the reproduction on the original code (%s); the "
                    "claim is inconclusive (still L1)." % original["status"]
                )
            return result

        level = EvidenceLevel.L2_REPRODUCED
        patched = self._run(patched_files, reproduction_command)
        result["steps"].append({"step": "reproduce-on-patched", **patched})
        if patched["status"] == "passed":
            level = EvidenceLevel.L3_FIX_VERIFIED
            if regression_command:
                regression = self._run(patched_files, regression_command)
                result["steps"].append({"step": "regression-on-patched", **regression})
                if regression["status"] == "passed":
                    level = EvidenceLevel.L4_REGRESSION_CLEAN
                elif regression["status"] == "failed":
                    result["note"] = (
                        "Fix verified against the reproduction, but the regression "
                        "suite failed (capped at L3)."
                    )
                else:
                    result["note"] = (
                        "Fix verified against the reproduction, but the regression "
                        "suite could not be run (%s); L4 not claimed." % regression["status"]
                    )
            else:
                result["note"] = (
                    "Fix verified against the reproduction; no regression command "
                    "was provided, so L4 is not claimed."
                )
        elif patched["status"] == "failed":
            result["note"] = (
                "Issue reproduced on the original code, but the patch did not make "
                "the reproduction pass (capped at L2)."
            )
        else:
            result["note"] = (
                "Issue reproduced on the original code, but the patch could not be "
                "verified (%s); capped at L2." % patched["status"]
            )

        result["evidence_level"] = int(level)
        result["evidence_label"] = LEVEL_LABELS[level]
        return result

    def _run(self, files: dict[str, str], command: str) -> dict:
        try:
            outcome = self.executor.execute(files, command)
            if not isinstance(outcome, dict):
                raise TypeError("proof executor result must be a mapping")
            checks = outcome.get("checks", [])
            if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
                raise TypeError("proof executor checks must be a list of mappings")
            passed = outcome.get("passed")
            if not isinstance(passed, bool):
                raise TypeError("proof executor passed verdict must be boolean")
            status = outcome.get("status") or ("passed" if passed else "failed")
            if status not in {"passed", "failed", "timeout", "error"} or passed != (
                status == "passed"
            ):
                raise TypeError("proof executor returned an inconsistent status")
            duration = outcome.get("duration_seconds", 0.0)
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not 0 <= duration < float("inf")
            ):
                raise TypeError("proof executor duration must be finite and non-negative")
            detail = ""
            for check in checks:
                if "detail" in check and not isinstance(check["detail"], str):
                    raise TypeError("proof executor check detail must be a string")
                if check.get("detail"):
                    detail = check["detail"]
                    break
            return {
                "passed": passed,
                "status": status,
                "detail": detail,
                "duration_seconds": duration,
                **(
                    {"attestation": outcome["attestation"]}
                    if isinstance(outcome.get("attestation"), dict)
                    else {}
                ),
            }
        except Exception as exc:
            return {
                "passed": False,
                "status": "error",
                "detail": safe_exception_summary(exc, "proof executor failed"),
                "duration_seconds": 0.0,
            }


def _materialize(files: dict[str, str], root: str) -> None:
    root = os.path.abspath(root)
    for rel, content in files.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise ClientInputError("proof file set must be a mapping of str paths to str content")
        target = _proof_target(root, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)


def _proof_target(root: str, rel: str) -> str:
    root = os.path.abspath(root)
    if not isinstance(rel, str) or not rel or "\0" in rel or os.path.isabs(rel):
        raise ClientInputError("unsafe path in proof file set: %s" % rel)
    target = os.path.abspath(os.path.join(root, rel))
    if target == root or not target.startswith(root + os.sep):
        raise ClientInputError("unsafe path in proof file set: %s" % rel)
    return target
