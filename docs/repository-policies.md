# Tenant and repository policies

Repository policy is the application decision boundary between tenant
governance and review execution. It is stored independently from the legacy
repository allowlist.

## Policy document

```json
{
  "enabled": true,
  "auto_fix": false,
  "post_review_comments": true,
  "allowed_reviewers": ["multi-agent-collaboration"],
  "allowed_fix_rules": ["SEC-YAML-LOAD", "SEC-INSECURE-COOKIE"],
  "allowed_llm_providers": ["local"],
  "allowed_llm_models": [],
  "llm_region": null,
  "max_diff_bytes": 524288
}
```

An empty allowlist means “all providers currently installed in that category”;
it does not mean “none”. Unknown fields, duplicate identifiers, ambiguous
boolean/integer values, unavailable FixRules, and non-positive Diff limits are
rejected before persistence.

| Field | Enforcement point |
| --- | --- |
| `enabled` | Intake, queued execution, and repair publication |
| `max_diff_bytes` | API Diff intake and webhook Diff fetch, in addition to the global cap |
| `allowed_reviewers` | Whole configured review-pipeline identity at intake and execution |
| `allowed_llm_providers` / `allowed_llm_models` | Intake and queued execution |
| `llm_region` | Intake route eligibility and every gateway call |
| `post_review_comments` | GitHub comment publication |
| `auto_fix` | Repair publication |
| `allowed_fix_rules` | Findings eligible for deterministic repair |

`llm_region` can bind sensitive repositories to the configured route's exact
region identifier. Provider and model allowlists are evaluated at intake and
again inside the gateway.

`allowed_reviewers` is intentionally pipeline-level: the default value is
`multi-agent-collaboration`, and unavailable names are rejected when a policy
is saved. It does not select individual specialists or Skills inside that
coordinator; production Skill membership is governed by the immutable deployed
reviewer revision.

## Version and execution semantics

Every update atomically:

1. locks the tenant/repository policy key;
2. increments its version;
3. replaces the current normalized document;
4. appends an immutable history row with the actor;
5. writes `repository-policy.updated` to the tenant audit log.

Review intake stores the selected version and normalized policy in the task
input. Retries therefore use the same reviewer/model/Diff/publication decision
even if an administrator later edits the policy. One exception is the emergency
kill switch: queued execution also reads the current policy and stops when the
repository is now disabled. Comment publication rechecks comment permission;
verified repair publication rechecks `enabled`, `auto_fix` and every applied
rule at the provider write boundary, after any existing-comment scan. Session
state is checked at the same boundary so closed, draft, or superseded turns do
not publish.

Existing installations remain backward compatible. If no versioned policy
exists for a repository, the resolver delegates to `repository_grants` with its
existing semantics. Saving the first versioned policy takes precedence for that
tenant/repository key. Repository identity follows GitHub semantics: validated
`owner/name` values are stored and compared in lowercase, while existing
mixed-case policy and grant rows remain readable.

## API

Both operations require the `manage` permission and are tenant-scoped from the
authenticated principal; callers cannot choose another tenant in the body.

```bash
curl -X POST http://127.0.0.1:8080/v1/repository-policies \
  -H 'Authorization: Bearer <token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "repository": "acme/payments",
    "policy": {
      "enabled": true,
      "auto_fix": false,
      "post_review_comments": true,
      "allowed_llm_providers": ["local"],
      "max_diff_bytes": 524288
    }
  }'

curl 'http://127.0.0.1:8080/v1/repository-policies?repository=acme%2Fpayments' \
  -H 'Authorization: Bearer <token>'
```

The read response contains the effective source (`configured` or
`legacy-grant`), current normalized document, and bounded newest-first version
history. Policy updates do not expose a delete operation: disable explicitly or
publish a new version so the audit trail remains intact.
