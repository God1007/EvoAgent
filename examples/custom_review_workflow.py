"""Run without Docker, a database or an LLM: python examples/custom_review_workflow.py.

Keep every default evidence gate, then append a custom business-policy agent.
Add --serve to start the actual API with normal EVOAGENT_* deployment settings.
Use --workflow FILE to assemble registered agents from a trusted JSON snapshot.
"""

import argparse
import hashlib
import json
import sys
from functools import partial
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evoagent.agents import FINDINGS_TYPE, MultiAgentCoordinator, WorkflowFactory, review_workflow
from evoagent.diff_parser import parse_unified_diff
from evoagent.json_boundary import strict_json_loads
from evoagent.reviewer import LocalRuleReviewer
from evoagent.workflow import MAX_HANDOFF_BYTES, AgentSpec, Handoff, Step, Workflow

MIN_CONFIDENCE = 0.8


def business_policy(handoff: Handoff) -> dict:
    handoff.check_active()
    # Only detached, explicitly wired findings are visible here, not the entire task.
    return {
        "findings": [
            item for item in handoff.inputs["findings"] if item["confidence"] >= MIN_CONFIDENCE
        ]
    }


def build_workflow(catalog, *, definition: dict | None = None) -> Workflow:
    default = review_workflow(catalog)
    # Bind both implementation and behavior-affecting configuration. Never hash secrets.
    revision = hashlib.sha256(
        Path(__file__).read_bytes() + str(MIN_CONFIDENCE).encode()
    ).hexdigest()
    policy = AgentSpec(
        "business-policy",
        revision,
        {"findings": FINDINGS_TYPE},
        {"findings": FINDINGS_TYPE},
        business_policy,
    )
    if definition is not None:
        return Workflow.from_dict(
            definition, {**catalog, "business-policy": policy}, default.inputs
        )
    return Workflow(
        "business-review",
        default.inputs,
        (*default.steps, Step("business", policy, {"findings": default.outputs["verified"]})),
        {"verified": "business.findings"},
    )


def load_workflow_factory(path: Path) -> WorkflowFactory:
    # Read once at startup; rebuilding a prompt lane must not reload edited wiring.
    with path.open("rb") as handle:
        content = handle.read(MAX_HANDOFF_BYTES + 1)
    if len(content) > MAX_HANDOFF_BYTES:
        raise ValueError("workflow definition exceeds the size limit")
    definition = strict_json_loads(content)
    if not isinstance(definition, dict):
        raise ValueError("workflow definition must be a JSON object")
    return partial(build_workflow, definition=definition)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", type=Path, help="Trusted JSON wiring; no Python imports")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="Start the configured EvoAgent API")
    mode.add_argument("--check", action="store_true", help="Validate wiring without running agents")
    args = parser.parse_args(argv)
    try:
        factory = load_workflow_factory(args.workflow) if args.workflow else build_workflow
        reviewer = MultiAgentCoordinator([LocalRuleReviewer()], workflow_factory=factory)
    except (OSError, ValueError, RecursionError) as exc:
        parser.error(str(exc))
    if args.serve:
        from evoagent.api import run

        run(workflow_factory=factory)
        return
    if args.check:
        print(
            json.dumps(
                {
                    "valid": True,
                    "revision": reviewer.workflow.revision,
                    "workflow": reviewer.workflow.describe(),
                },
                indent=2,
            )
        )
        return
    diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+eval(user_input)\n"
    findings = reviewer.review(diff, parse_unified_diff(diff))
    print(
        json.dumps(
            {
                "revision": reviewer.workflow.revision,
                "workflow": reviewer.workflow.describe(),
                "findings": [item.to_dict() for item in findings],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
