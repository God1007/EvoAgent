"""Browser response allowlists, separate from the existing diagnostic API shapes.

This minimizes transported data; it does not replace authorization or detect
secrets in user-authored code/text. Unknown endpoints and artifact types never
fall back to serializing internal records.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .errors import ClientInputError

TASK_FIELDS = (
    "id",
    "state",
    "repository",
    "pull_request",
    "created_at",
    "updated_at",
    "retrying",
    "cancel_requested",
)
FINDING_FIELDS = ("severity", "title", "explanation", "path", "line", "evidence", "fix", "test")
DOCUMENT_FIELDS = ("id", "revision", "active_version", "name", "updated_at")
VERSION_FIELDS = ("version", "draft_revision", "created_at")
POLICY_FIELDS = (
    "enabled",
    "auto_fix",
    "post_review_comments",
    "allowed_reviewers",
    "allowed_fix_rules",
    "allowed_llm_providers",
    "allowed_llm_models",
    "llm_region",
    "max_diff_bytes",
)

# Exact reviewed messages only. Never interpolate exception text or payloads into
# the console response; new/unrecognized failures use the HTTP-status fallback.
# ponytail: reuse existing messages; move codes into error types if this catalog grows.
ERROR_CODES = {
    "repository must use the GitHub owner/name form": "invalid_repository",
    "diff is required": "diff_required",
    "pull_request must be a positive integer": "invalid_pull_request",
    "pull_request must be a positive PostgreSQL integer": "invalid_pull_request",
    "Idempotency-Key was already used with a different review": "submission_conflict",
    "invalid username or password": "login_failed",
    "user is not a member of the requested tenant": "login_failed",
    "authentication is disabled": "authentication_disabled",
    "this endpoint has no console view": "unsupported_view",
    "draft changed or does not exist; reload before saving": "draft_conflict",
    "draft changed; reload before publishing": "draft_conflict",
    "draft changed; save and retry": "draft_conflict",
    "仓库配置已变化，请重新读取后再切换。": "binding_conflict",
    "repository policy changed; reload before saving": "policy_conflict",
    "草稿结构暂不受编辑器支持，原始内容未修改。": "unsupported_draft",
    "published agent digest mismatch": "invalid_version",
    "published workflow digest mismatch": "invalid_version",
    "selected model is unavailable; configure it before running": "model_unavailable",
    "cancelled task cannot be resumed": "task_cancelled",
    "review was cancelled": "task_cancelled",
    "task payload is no longer available": "payload_unavailable",
    "task delivery cannot be resumed": "delivery_unavailable",
    "automatic repair is not enabled by repository policy": "fix_not_allowed",
    "pull request head changed; run a new review before fixing": "review_outdated",
    "pull request is no longer open; run a new review before fixing": "pr_closed",
    "pull request is a draft; mark it ready before fixing": "pr_draft",
    "port and step names must be 1-64 character alphanumeric tokens": "invalid_identifier",
    "draft step ids must be unique": "duplicate_step",
    "unknown handoff type": "unknown_handoff_type",
    "rule agents take diff and return findings": "invalid_rule_ports",
    "merge agents take findings ports and return findings; no config": "invalid_merge_ports",
    "select installed rules and/or at most 32 literal checks": "invalid_checks",
    "an agent supports at most 32 literal checks": "invalid_checks",
    "only the listed read-only tools may be selected": "invalid_tools",
    "diff tools require an explicitly connected diff input": "diff_input_required",
    "model findings require a diff input to verify their locations": "diff_input_required",
    "max_output_tokens must be between 1 and 4096": "invalid_output_limit",
    "model outputs cannot impersonate source review artifacts": "invalid_model_output",
    "model config requires a Playbook, model, tools and max_output_tokens": "invalid_playbook",
    "Playbook requires identity, objective and instructions": "invalid_playbook",
    "Playbook identity must be text of 1-200 characters": "invalid_playbook",
    "Playbook objective must be text of 1-2000 characters": "invalid_playbook",
    "Playbook instructions must be text of 1-12000 characters": "invalid_playbook",
    "prompt must be text of 1-16000 characters": "invalid_prompt",
    "agent name must be text of 1-100 characters": "invalid_name",
    "workflow name must be text of 1-100 characters": "invalid_name",
    "model must be text of 1-200 characters": "model_required",
    "workflow requires 1-64 steps": "workflow_steps",
    "workflow exceeds 64 steps": "workflow_steps",
    "workflow must contain steps": "workflow_steps",
    "workflow draft steps must be an array of at most 64 nodes": "workflow_steps",
    "invalid workflow: check missing ports, connections, types and cycles": "invalid_workflow",
    "review workflows must expose verified as review-findings@1": "report_output_required",
    "every step must connect to a workflow output": "disconnected_step",
    "tenant review capacity is exhausted": "review_capacity",
}


def console_error(status: int, value: dict) -> dict[str, str]:
    defaults = {
        400: "invalid_request",
        401: "authentication_required",
        403: "access_denied",
        404: "not_found",
        409: "state_conflict",
        429: "rate_limited",
        503: "unavailable",
    }
    message = value.get("error")
    code = ERROR_CODES.get(message) if isinstance(message, str) and status < 500 else None
    return {"error_code": code or defaults.get(status, "internal_error")}


def pick(value: dict | None, *keys: str) -> dict:
    return {key: value[key] for key in keys if value is not None and key in value}


def _template(value: dict) -> dict:
    raw_definition = value.get("definition")
    definition = raw_definition if isinstance(raw_definition, dict) else {}
    return {
        **pick(value, "id", "name", "description", "available", "reason"),
        "definition": {
            **pick(definition, "name", "outputs"),
            "steps": [
                pick(step, "id", "agent", "version", "sources")
                for step in definition.get("steps", [])
                if isinstance(step, dict)
            ],
        },
    }


def _agent_recipe(value: dict) -> dict:
    raw_definition = value.get("definition")
    definition = raw_definition if isinstance(raw_definition, dict) else {}
    raw_config = definition.get("config")
    config = raw_config if isinstance(raw_config, dict) else {}
    raw_playbook = config.get("playbook")
    playbook = raw_playbook if isinstance(raw_playbook, dict) else {}
    return {
        **pick(value, "id", "name", "description"),
        "definition": {
            **pick(definition, "name", "kind", "inputs", "outputs"),
            "config": {
                "playbook": pick(playbook, "identity", "objective", "instructions"),
                **pick(config, "model", "tools", "max_output_tokens"),
            },
        },
    }


def _task(value: dict) -> dict:
    result = pick(value, *TASK_FIELDS)
    report = value.get("report")
    result["report"] = None
    if isinstance(report, dict):
        result["report"] = pick(report, "risk", "files_reviewed")
        if isinstance(report.get("findings"), list):
            result["report"]["findings"] = [
                pick(item, *FINDING_FIELDS) for item in report["findings"]
            ]
    task_input = value.get("input") or {}
    result["cancel_requested"] = value.get("cancel_requested") is True
    result["delivery_pending"] = bool(
        value.get("state") == "SUCCESS"
        and (task_input.get("session_id") or task_input.get("github_issue_url"))
        and task_input.get("_delivery_complete") is not True
    )
    # State hints only: each operation rechecks authorization and state atomically.
    result["can_cancel"] = not result["cancel_requested"] and (
        value.get("state") in {"PENDING", "PLANNING", "EXECUTING", "REVIEWING"}
        or value.get("retrying") is True
    )
    result["can_resume"] = not result["cancel_requested"] and (
        (value.get("state") == "FAILED" and not value.get("retrying")) or result["delivery_pending"]
    )
    result["fix_blocker"] = value.get("fix_blocker", "unknown")
    if not isinstance(result["fix_blocker"], str) or result["fix_blocker"] not in {
        "",
        "permission",
        "pr_snapshot",
        "policy",
        "rules",
        "installation",
        "github",
        "sandbox",
        "tests",
    }:
        result["fix_blocker"] = "unknown"
    result["can_fix"] = bool(
        value.get("state") == "SUCCESS"
        and report
        and value.get("pull_request")
        and task_input.get("head_sha")
        and result["fix_blocker"] == ""
    )  # Capability hint only; POST /fix still authorizes and verifies the operation.
    pinned = task_input.get("studio_workflow")
    if isinstance(pinned, dict):
        bundle = pinned.get("bundle") or {}
        definition, agents = bundle.get("definition") or {}, bundle.get("agents") or {}
        result["workflow"] = {
            **pick(pinned, "version", "draft_revision"),
            "name": definition.get("name", ""),
            "steps": {
                step["id"]: agents.get("%s_v%s" % (step["agent"], step["version"]), {}).get(
                    "name", ""
                )
                for step in definition.get("steps", [])
            },
        }
    return result


def _workflow(value: dict) -> dict:
    result = pick(value, "availability", "task_state", "artifacts_pruned_at")
    result["workflow"] = pick(value.get("workflow"), "name") if value.get("workflow") else None
    result["steps"] = [
        {
            **pick(
                step,
                "id",
                "inputs",
                "outputs",
                "sources",
                "status",
                "blocked_by",
                "attempt",
                "duration_ms",
                "updated_at",
            ),
            "error": bool(step.get("error")),
        }
        for step in value.get("steps", [])
    ]
    return result


def _artifact_value(value: Any, kind: str) -> Any:
    if kind == "review-findings@1" and isinstance(value, list):
        return [pick(item, *FINDING_FIELDS) for item in value]
    if kind in {"unified-diff@1", "text@1"} and isinstance(value, str):
        return value
    if (kind == "integer@1" and type(value) is int) or (
        kind == "boolean@1" and type(value) is bool
    ):
        return value
    if kind == "parsed-diff@1" and isinstance(value, dict):
        return {
            "files": value.get("files", []),
            "added_line_count": len(value.get("added_lines", [])),
        }
    if kind == "review-context@1" and isinstance(value, dict):
        return pick(value, "origin", "title", "spec", "standards", "truncated")
    if kind == "repository-evidence@1" and isinstance(value, dict):
        return pick(
            value,
            "origin",
            "status",
            "revision",
            "indexed_files",
            "indexed_bytes",
            "changed_paths",
            "changed_symbols",
            "impacted_symbols",
            "importing_files",
            "truncated",
        )
    if kind == "review-plan@1" and isinstance(value, dict):
        return {
            **pick(value, "languages", "changed_files"),
            "assignment_count": len(value.get("assignments", [])),
        }
    if kind in {
        "review-critiques@1",
        "review-reproductions@1",
        "review-fix-decisions@1",
    } and isinstance(value, dict):
        field = "accepted" if kind == "review-critiques@1" else "reproducible"
        return {
            "checked": len(value),
            "accepted": sum(
                item is True if kind == "review-fix-decisions@1" else item.get(field) is True
                for item in value.values()
            ),
        }
    return None


def _artifact(value: dict) -> dict:
    result = pick(value, "step_id", "status", "unavailable_inputs")
    for direction in ("inputs", "outputs"):
        payloads = value.get(direction) or {}
        types = (value.get("port_types") or {}).get(direction, {})
        result[direction] = {
            port: _artifact_value(payloads[port], kind)
            for port, kind in types.items()
            if port in payloads and (direction == "inputs" or value.get("status") == "completed")
        }
    return result


def _document(value: dict) -> dict:
    # An explicitly opened editor needs its authored definition; the published
    # palette and task reports do not need the Prompt/config stored inside it.
    return {
        **pick(value, *DOCUMENT_FIELDS, "definition"),
        "versions": [pick(version, *VERSION_FIELDS) for version in value.get("versions", [])],
    }


def _version(value: dict) -> dict:
    return {
        **pick(value, *VERSION_FIELDS),
        "definition": pick(value.get("definition"), "name", "kind", "inputs", "outputs"),
    }


def _evaluation(value: dict) -> dict:
    return {
        **pick(value, "decision"),
        "version": pick(value.get("version"), "version"),
        "candidate": pick(value.get("candidate"), "score"),
        "baseline": pick(value.get("baseline"), "score"),
    }


def _proof(value: dict) -> dict:
    return {
        **pick(value, "evidence_level", "patch"),
        "steps": [
            {
                **pick(step, "step", "status", "duration_seconds"),
                # Infrastructure diagnostics are not test evidence or user-facing prose.
                "detail": step.get("detail", "")
                if step.get("status") in {"passed", "failed"}
                else "",
            }
            for step in value.get("steps", [])
        ],
    }


def _audit_events(value: dict) -> dict:
    return {
        "events": [
            pick(event, "actor", "action", "resource", "created_at")
            for event in value.get("events", [])
            if isinstance(event, dict)
        ]
    }


def _outbox_messages(value: dict) -> dict:
    return {
        "messages": [
            {
                **pick(
                    message,
                    "id",
                    "status",
                    "attempts",
                    "available_at",
                    "created_at",
                    "updated_at",
                ),
                "error": bool(message.get("last_error")),
            }
            for message in value.get("messages", [])
            if isinstance(message, dict)
        ]
    }


def _dead_letters(value: dict) -> dict:
    messages = []
    for message in value.get("messages", []):
        if not isinstance(message, dict):
            continue
        payload = message.get("payload")
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        messages.append(
            {
                **pick(message, "attempt", "failed_at"),
                "task_id": task_id if isinstance(task_id, str) else None,
                "error": bool(message.get("error")),
            }
        )
    return {"messages": messages}


def _repository_policy(value: dict) -> dict:
    policy = value.get("policy")
    return {
        **pick(value, "repository", "version", "source", "updated_at"),
        "policy": pick(policy, *POLICY_FIELDS) if isinstance(policy, dict) else {},
        "history": [
            pick(item, "version", "actor", "created_at")
            for item in value.get("history", [])
            if isinstance(item, dict)
        ],
        "available_fix_rules": [
            item for item in value.get("available_fix_rules", []) if isinstance(item, str)
        ],
        "available_reviewers": [
            item for item in value.get("available_reviewers", []) if isinstance(item, str)
        ],
    }


# Only the console's actual requests have a view. This is not a second router:
# existing handlers still own authorization, validation, queries and mutations.
VIEWS: tuple[tuple[str, Callable[[dict], dict]], ...] = (
    (
        r"GET /api/dashboard",
        lambda data: {
            "stats": pick(
                data.get("stats"),
                "tasks_total",
                "tasks_success",
                "tasks_failed",
                "success_rate",
                "unresolved_failure_cases",
                "active_skill_versions",
            ),
            "tasks": [pick(task, *TASK_FIELDS) for task in data.get("tasks", [])],
            "capabilities": pick(
                data.get("capabilities"),
                "role",
                "review",
                "manage",
                "audit",
                "platform",
                "proof",
                "github_install_configured",
            ),
        },
    ),
    (
        r"GET /api/tasks",
        lambda data: {"tasks": [pick(task, *TASK_FIELDS) for task in data.get("tasks", [])]},
    ),
    (
        r"GET /api/skills",
        lambda data: {
            "skills": [
                pick(skill, "name", "description", "version", "sandboxed")
                for skill in data.get("skills", [])
            ]
        },
    ),
    (
        r"GET /api/failures",
        lambda data: {
            "cases": [
                pick(case, "category", "created_at", "resolved") for case in data.get("cases", [])
            ]
        },
    ),
    (r"GET /api/audit", _audit_events),
    (r"GET /api/outbox", _outbox_messages),
    (r"GET /api/queue/dead-letters", _dead_letters),
    (
        r"GET /api/tenant-review-capacity",
        lambda data: pick(
            data,
            "enabled",
            "max_active_reviews",
            "active_reviews",
            "available",
            "saturated",
            "oldest_acquired_at",
        ),
    ),
    (r"GET /v1/repository-policies", _repository_policy),
    (
        r"GET /v1/evolution/status",
        lambda data: pick(
            data,
            "ready",
            "model_configured",
            "validation_cases",
            "holdout_cases",
            "minimum_cases",
            "minimum_holdout_cases",
        ),
    ),
    (
        r"GET /v1/evolution/runs",
        lambda data: {
            "runs": [
                pick(run, "candidate_version", "candidate_score", "baseline_score", "decision")
                for run in data.get("runs", [])
            ]
        },
    ),
    (r"GET /v1/tasks/[0-9a-f-]+", _task),
    (r"GET /v1/tasks/[0-9a-f-]+/workflow", _workflow),
    (r"GET /v1/tasks/[0-9a-f-]+/workflow/[A-Za-z0-9_-]+", _artifact),
    (
        r"GET /v1/studio/catalog",
        lambda data: {
            **pick(data, "types", "inputs", "tools"),
            "rules": [pick(rule, "id", "title") for rule in data.get("rules", [])],
            "builtins": [
                pick(agent, "id", "version", "inputs", "outputs")
                for agent in data.get("builtins", [])
            ],
            "models": [pick(model, "provider", "model") for model in data.get("models", [])],
            "agent_recipes": [_agent_recipe(recipe) for recipe in data.get("agent_recipes", [])],
            "templates": [_template(template) for template in data.get("templates", [])],
        },
    ),
    (
        r"GET /v1/studio/(agents|workflows)",
        lambda data: {
            **pick(data, "next_cursor"),
            "documents": [pick(item, *DOCUMENT_FIELDS) for item in data.get("documents", [])],
        },
    ),
    (r"GET /v1/studio/(agents|workflows)/[0-9a-f]+", _document),
    (r"GET /v1/studio/agents/[0-9a-f]+/versions/[0-9]+", _version),
    (
        r"GET /v1/studio/workflows/[0-9a-f]+/versions/[0-9]+",
        lambda data: {
            **pick(data, *VERSION_FIELDS),
            "name": data.get("definition", {}).get("definition", {}).get("name"),
        },
    ),
    (r"POST /v1/auth/login", lambda data: pick(data, "access_token", "role")),
    (r"POST /v1/github/installations", lambda data: pick(data, "url")),
    (r"POST /v1/reviews", lambda data: pick(data, "task_id", "state", "replayed")),
    (r"POST /v1/proofs", _proof),
    (
        r"POST /v1/tasks/[0-9a-f-]+/resume",
        lambda data: pick(
            data,
            "task_id",
            "state",
            "resumed",
            "already_active",
            "delivery_resumed",
            "delivery_already_active",
            "delivery_complete",
        ),
    ),
    (
        r"POST /v1/tasks/[0-9a-f-]+/cancel",
        lambda data: pick(data, "accepted", "cancel_requested", "state"),
    ),
    (r"POST /v1/tasks/[0-9a-f-]+/fix", lambda data: pick(data, "branch")),
    (r"POST /v1/outbox/replay", lambda data: pick(data, "replayed")),
    (r"POST /v1/repository-policies", _repository_policy),
    (r"POST /v1/evolution/(propose|auto)", _evaluation),
    (r"POST /v1/studio/(agents|workflows)", _document),
    (
        r"POST /v1/studio/(agents|workflows)/[0-9a-f]+/publish",
        lambda data: pick(data, *VERSION_FIELDS),
    ),
    (r"POST /v1/studio/validate", lambda data: pick(data, "valid")),
    (
        r"(GET|POST) /v1/studio/binding",
        lambda data: {
            "binding": pick(
                data.get("binding"), "workflow_id", "version", "revision", "name", "updated_at"
            )
            if data.get("binding")
            else None
        },
    ),
)


def console_response(method: str, path: str, value: dict | None = None) -> dict | None:
    """With no value, validate view support before any handler side effect."""
    project = next(
        (handler for pattern, handler in VIEWS if re.fullmatch(pattern, method + " " + path)), None
    )
    if project is None:
        raise ClientInputError("this endpoint has no console view")
    return project(value) if value is not None else None
