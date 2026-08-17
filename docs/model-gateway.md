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
  -> atomically claim shared route concurrency/rate capacity
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

Schema version 12 stores request id, root request id, route id, attempt number,
tenant/repository/task scope, purpose,
provider/model, reserved and actual usage, micro-unit cost, redaction count,
SHA-256 of the redacted canonical request, active/shadow lane, topology hash,
timestamps, status, and sanitized error text. Shadow observations separately
store route/scope IDs, input/output hashes, agreement, usage, duration, status,
and message-free error type/reference. Neither table stores message or response
content, API keys, or custom provider headers. Capacity tables contain only
route/topology/request identifiers, time-bucket counters, and expiring leases.

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
- `EVOAGENT_LLM_CAPACITY_LEASE_SECONDS` defaults to 180 and must exceed the
  provider timeout. It bounds crash-held concurrency without cancelling a live call.
- `EVOAGENT_LLM_CAPACITY_WINDOW_RETENTION_HOURS` defaults to 48 and controls
  retention of minute counters; it does not change enforcement semantics.
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
capacity_max_inflight = 24
capacity_requests_per_minute = 300

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
429, 5xx, open circuits, output-contract failures, and route-capacity
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
capacity_max_inflight = 24
capacity_requests_per_minute = 300

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
capacity_max_inflight = 4
capacity_requests_per_minute = 40
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

### Distributed route capacity (v2)

`capacity_max_inflight` and `capacity_requests_per_minute` are optional hard
admission ceilings. Zero disables that dimension. Admission occurs before the
usage reservation and provider call, so a rejected route creates neither a
provider request nor a misleading usage row. The next already-ordered route may
run only within `EVOAGENT_LLM_FALLBACK_ATTEMPTS`.

SQLite provides equivalent single-node development behavior. PostgreSQL uses a
transaction advisory lock plus durable leases and fixed UTC-minute counters, so
all API/worker replicas share one decision. The stable `route_id` is the
capacity-pool identity across topology hashes: old and new deployments therefore
cannot each consume a full allowance during a rolling release. Route IDs must
not be reused for unrelated provider pools. A dead process's concurrency lease
expires conservatively; minute admissions are never refunded. Because admission
precedes budget enforcement, a later budget rejection may consume one rate unit,
favoring provider protection over maximum utilization.

Administrators can inspect capacity, rejection counters, breaker state, and
read-only weight recommendations derived from declared capacity:

```bash
curl 'http://127.0.0.1:8080/api/model-routes/capacity?repository=acme%2Fpayments' \
  -H 'Authorization: Bearer <admin-token>'
```

Recommendations normalize all fully-capacity-declared active routes in the same
priority tier by requests/minute, or by max in-flight when every route declares
that dimension. They never mutate live topology. Operators review the result,
change weights in version control, and redeploy. See
[`ADR 0022`](adr/0022-distributed-model-route-capacity.md).

Because enforcement is global to a provider pool, exact counters on a route
shared by multiple tenants would reveal cross-tenant activity. The tenant API
therefore returns `observation_scope=shared-redacted` and only its availability
for shared routes. Exact counters are returned only when `tenant_ids` binds the
route to exactly the requesting tenant; platform-wide investigation uses the
restricted operational database/telemetry boundary.

## Extension and current boundary

An enterprise deployment can replace plugin id `evoagent.model-gateway` and
provide the same `ModelGatewayPort`, without changing the review graph. The
built-in implementation supports declarative selection, residency, bounded
provider fallback, deterministic weighted routing, isolated candidate shadows,
GitOps promotion gates, per-route breakers, and conservative crash
reconciliation. It also supports database-coordinated capacity fallback and
declared-capacity weight recommendations. It does not auto-apply weights or infer
future provider capacity from traffic.
