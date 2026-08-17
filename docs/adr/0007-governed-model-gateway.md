# ADR 0007: Governed model gateway and metadata-only usage ledger

- Status: accepted
- Date: 2026-08-17

## Context

The original OpenAI-compatible reviewer owned its URL, API key, HTTP call, and
response parsing. That made it easy to add a model but impossible to enforce
tenant/repository budgets, prove where source code could leave the service, or
replace routing independently from the review graph. Parallel specialist calls
also lacked task scope, so accounting after the fact would have been ambiguous.

## Decision

Introduce the stable `model.gateway` capability and `ModelGatewayPort`.
`GatewayReviewer` sends typed, task-scoped requests; the multi-agent coordinator
propagates task context into worker threads explicitly. The built-in gateway:

- removes common credentials before transport and sanitizes persisted/raised
  upstream errors against route credentials;
- validates HTTPS and an exact-host allowlist, and caps input, output, and HTTP
  response size;
- requires a JSON object for review responses;
- atomically reserves a worst-case daily token/cost amount for one
  tenant/repository before calling the provider;
- reconciles calls to known actual usage, retaining billed output-gate failures
  while releasing transport failures for which no usage was returned;
- persists governance metadata and a redacted request hash, never prompt or
  response bodies.

Schema version 6 adds the ledger. Both stores implement the same sequential and
concurrent budget contracts. A tenant-scoped, manage-only API exposes the
metadata for operations.

Keep the legacy reviewer class for source compatibility and focused parser
tests, but remove it from production composition. Keep the default gateway
single-route in this increment and state that boundary explicitly.

## Consequences

- Reviewer and evolution code no longer require model credentials.
- Quota checks are race-safe across workers and instances.
- Provider replacement is independent of the review engine and is covered by a
  plugin composition test.
- Cost enforcement is meaningful only when operators configure pricing; startup
  rejects a non-zero cost budget with zero input/output prices.
- Redaction reduces accidental disclosure but does not replace organizational
  DLP, network egress controls, or provider retention contracts.
- A crash can leave a reservation consuming budget for the remainder of the UTC
  day. This is fail-closed; reconciliation is deferred.
- Multi-route fallback, per-route circuit breakers, residency policy, and route
  shadow promotion are intentionally not claimed by this decision.
