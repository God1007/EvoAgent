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
  -> filter ordered routes by tenant/repository/provider/model/region policy
  -> atomically reserve worst-case token/cost budget for the selected attempt
  -> validate HTTPS and exact destination host
  -> call provider through its independent circuit breaker
  -> use a bounded fallback route only for an eligible failure
  -> enforce response byte/token limits and JSON-object output
  -> reconcile durable usage to actual values or record sanitized failure
```

The reservation is intentionally pessimistic: estimated input plus the allowed
maximum output is counted before network I/O. On success it is replaced by
actual provider usage (or a conservative local estimate when usage is absent).
If a response consumes tokens but later fails the output gate, its known actual
usage remains chargeable; a transport failure with no usage information releases
the reservation. A process crash may leave a `reserved` row consuming that UTC
day's budget; this is fail-closed and clears at the next period, while explicit
stale-reservation recovery remains future operational work.

SQLite serializes reservation with `BEGIN IMMEDIATE`; PostgreSQL uses a
transaction advisory lock keyed by tenant/repository/day. Therefore concurrent
workers cannot both pass a quota check and overspend the same budget.

## Stored data and access

Schema version 7 stores request id, root request id, route id, attempt number,
tenant/repository/task scope, purpose,
provider/model, reserved and actual usage, micro-unit cost, redaction count,
SHA-256 of the redacted canonical request, timestamps, status, and sanitized
error text. It does **not** store message or response content, API keys, or
custom provider headers.

Administrators can query the current tenant only:

```bash
curl 'http://127.0.0.1:8080/api/model-usage?repository=acme%2Fpayments&limit=100' \
  -H 'Authorization: Bearer <token>'
```

## Configuration semantics

- `EVOAGENT_LLM_ALLOWED_HOSTS` contains exact DNS names. When empty, only the
  configured route's own host is trusted for backward compatibility.
- HTTPS is mandatory except for explicit loopback development endpoints.
- token and cost budgets are scoped per tenant/repository/UTC day; `0` disables
  the corresponding budget.
- cost uses integer micro units. Enabling a cost budget requires at least one
  non-zero input/output price per million tokens.
- redaction covers common secret assignments, Bearer values, PEM private keys,
  and configured route credentials in errors. It is a guardrail, not a complete
  data-loss-prevention engine.

### Multi-route topology

Set `EVOAGENT_LLM_ROUTES_FILE` to a trusted version-1 TOML file. Routes are
ordered by ascending priority. Secret values are never stored in TOML; each
route names an environment variable through `api_key_env`.

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
LLM breaker metric reports the worst route state.

## Extension and current boundary

An enterprise deployment can replace plugin id `evoagent.model-gateway` and
provide the same `ModelGatewayPort`, without changing the review graph. The
built-in v0.10 implementation supports declarative selection, residency,
bounded provider fallback, and per-route breakers. Candidate-route shadowing
and promotion, weighted load balancing, and stale-reservation reconciliation
are not yet claimed.
