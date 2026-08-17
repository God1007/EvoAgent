# Governed model gateway

The production review path never passes endpoint credentials into a reviewer.
`GatewayReviewer` submits a typed `ModelRequest` to the replaceable
`model.gateway` capability with tenant, repository, task, purpose, messages,
structured-output requirement, and output limit.

## Request lifecycle

```text
task context
  -> redact likely credentials
  -> canonicalize + hash (content is not persisted)
  -> enforce estimated input limit
  -> filter active routes by tenant/repository/provider/model/region policy
  -> deterministically weight routes inside each priority tier
  -> atomically reserve worst-case token/cost budget for the selected attempt
  -> validate HTTPS and exact destination host
  -> call provider through its independent circuit breaker
  -> use a bounded fallback route only for an eligible failure
  -> enforce response byte/token limits and JSON-object output
  -> reconcile durable usage to actual values or record sanitized failure
  -> schedule eligible candidate shadows without changing the active response
```

The reservation is intentionally pessimistic: estimated input plus the allowed
maximum output is counted before network I/O. On success it is replaced by
actual provider usage (or a conservative local estimate when usage is absent).
If a response consumes tokens but later fails the output gate, its known actual
usage remains chargeable; a transport failure with no usage information releases
the reservation. A process crash may leave a call without known provider usage.
At startup and during rate-limited maintenance, reservations older than
`EVOAGENT_LLM_RESERVATION_TTL_SECONDS` become `uncertain`. They continue to
consume their worst-case reserved Token and cost amounts; timeout alone never
releases possibly billed usage.

SQLite serializes reservation with `BEGIN IMMEDIATE`; PostgreSQL uses a
transaction advisory lock keyed by tenant/repository/day. Therefore concurrent
workers cannot both pass a quota check and overspend the same budget.

## Stored data and access

Schema version 11 stores request id, root request id, route id, attempt number,
tenant/repository/task scope, purpose,
provider/model, reserved and actual usage, micro-unit cost, redaction count,
SHA-256 of the redacted canonical request, active/shadow lane, topology hash,
timestamps, status, and sanitized error text. Shadow observations separately
store route/scope IDs, input/output hashes, agreement, usage, duration, status,
and message-free error type/reference. Neither table stores message or response
content, API keys, or custom provider headers.

Administrators can query the current tenant only:

```bash
curl 'http://127.0.0.1:8080/api/model-usage?repository=acme%2Fpayments&limit=100' \
  -H 'Authorization: Bearer <token>'
```

### Reconcile an uncertain reservation

An administrator first matches the request/route metadata to the provider's
billing or usage export. Only then can the tenant-scoped `uncertain` row be
settled to actual usage:

```bash
curl -X POST 'http://127.0.0.1:8080/v1/model-usage/reconcile' \
  -H 'Authorization: Bearer <admin-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "<request-id>",
    "status": "success",
    "input_tokens": 1240,
    "output_tokens": 183,
    "cost_micros": 417
  }'
```

The transition is allowed only from `uncertain`; it is idempotent by state and
cannot address another tenant. The ledger update and `model-usage.reconciled`
audit event commit in one database transaction. A verified zero-usage provider
record may use `failed` with zero counts; absence of evidence is not grounds to
release the conservative charge. Metrics expose expired and reconciled totals.

## Configuration semantics

- `EVOAGENT_LLM_ALLOWED_HOSTS` contains exact DNS names. When empty, only the
  configured route's own host is trusted for backward compatibility.
- HTTPS is mandatory except for explicit loopback development endpoints.
- token and cost budgets are scoped per tenant/repository/UTC day; `0` disables
  the corresponding budget.
- `EVOAGENT_LLM_RESERVATION_TTL_SECONDS` defaults to 600 and must exceed the
  configured provider/request timeout. Expiry quarantines; it does not forgive cost.
- cost uses integer micro units. Enabling a cost budget requires at least one
  non-zero input/output price per million tokens.
- redaction covers common secret assignments, Bearer values, PEM private keys,
  and configured route credentials in errors. It is a guardrail, not a complete
  data-loss-prevention engine.

### Multi-route topology

Set `EVOAGENT_LLM_ROUTES_FILE` to a trusted TOML file. Version 1 preserves
strict ascending-priority routing. Version 2 adds explicit lifecycle state,
weighted active routes, candidate shadows, and promotion evidence. Secret values
are never stored in TOML; each route names an environment variable through
`api_key_env`.

```toml
version = 1

[[routes]]
id = "eu-primary"
priority = 10
provider = "provider-a"
model = "model-a"
base_url = "https://eu-a.example/v1"
api_key_env = "PROVIDER_A_API_KEY"
region = "eu-west"
tenant_ids = ["acme"]
repository_patterns = ["acme/payments-*"]
input_cost_micros_per_million = 250000
output_cost_micros_per_million = 1000000

[[routes]]
id = "eu-fallback"
priority = 20
provider = "provider-b"
model = "model-b"
base_url = "https://eu-b.example/v1"
api_key_env = "PROVIDER_B_API_KEY"
region = "eu-west"
tenant_ids = ["acme"]
repository_patterns = ["acme/payments-*"]
```

`EVOAGENT_LLM_FALLBACK_ATTEMPTS` is the maximum number of additional routes,
not total calls. A value of `0` disables fallback. Transport timeouts, 408/425,
429, 5xx, open circuits, output-contract failures, and route-specific budget
rejections are eligible. Authentication/validation 4xx errors are not retried
through another provider. Each route owns a separate circuit breaker; the public
LLM breaker metric reports the worst **active** route state, so an experimental
candidate cannot make production readiness fail.

### Weighted active routes and candidate shadows (v2)

Routes in different priority tiers keep strict fallback order. Active routes
with the same priority use deterministic weighted sampling without replacement:
`weight = 3` receives approximately three times the primary assignments of
`weight = 1`, while the same task/request resolves consistently.

```toml
version = 2

[[routes]]
id = "eu-stable-a"
state = "active"
priority = 10
weight = 3
provider = "provider-a"
model = "model-a"
base_url = "https://eu-a.example/v1"
api_key_env = "PROVIDER_A_API_KEY"
region = "eu-west"
tenant_ids = ["acme"]
repository_patterns = ["acme/*"]

[[routes]]
id = "eu-candidate"
state = "candidate"
provider = "provider-c"
model = "model-c"
base_url = "https://eu-c.example/v1"
api_key_env = "PROVIDER_C_API_KEY"
region = "eu-west"
tenant_ids = ["acme"]
repository_patterns = ["acme/*"]
baseline_route_id = "eu-stable-a"
shadow_percent = 10
min_shadow_samples = 100
max_shadow_error_rate = 0.03
max_shadow_disagreement_rate = 0.15
# Replace these valid placeholders with approved immutable evidence digests.
evaluation_dataset_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
evaluation_report_sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
```

An active response is completed and accounted before any matching candidate is
scheduled. The bounded executor never returns candidate output to the reviewer.
`EVOAGENT_LLM_SHADOW_WORKERS` and `EVOAGENT_LLM_SHADOW_MAX_INFLIGHT` cap process
work; `EVOAGENT_LLM_SHADOW_DAILY_TOKEN_BUDGET` and
`EVOAGENT_LLM_SHADOW_DAILY_COST_MICROS` add candidate-only ceilings while the
normal total budget remains a hard upper bound. Zero disables the extra
candidate-only budget, not the total budget.

The observation row is inserted as `scheduled` before executor submission. On
startup and periodic gateway maintenance, rows older than the same reservation
TTL become terminal `uncertain` errors; they no longer block forever, but they
still count against the promotion error gate. A started provider call also
retains the normal conservative usage-reconciliation behavior.

Query one tenant/repository gate:

```bash
curl 'http://127.0.0.1:8080/api/model-routes/promotion?route_id=eu-candidate&repository=acme%2Fpayments' \
  -H 'Authorization: Bearer <admin-token>'
```

`eligible=true` means the configured live-operability thresholds pass, no
scheduled observation is pending, and both offline evidence digests are
present **for the requested tenant/repository scope**. Agreement with the stable
model is not independent quality evidence. A route shared by multiple scopes
must pass the report for every intended scope before global activation.
The endpoint never activates a route: promotion requires changing `state` to
`active` (and assigning a positive weight) in reviewed configuration, then
deploying the resulting new topology hash. See
[`ADR 0021`](adr/0021-gitops-model-route-promotion.md).

## Extension and current boundary

An enterprise deployment can replace plugin id `evoagent.model-gateway` and
provide the same `ModelGatewayPort`, without changing the review graph. The
built-in implementation supports declarative selection, residency, bounded
provider fallback, deterministic weighted routing, isolated candidate shadows,
GitOps promotion gates, per-route breakers, and conservative crash
reconciliation. Capacity-aware or automatically adjusted routing is not claimed.
