# ADR 0008: Policy-aware model routing with bounded fallback

- Status: accepted
- Date: 2026-08-17

## Context

ADR 0007 introduced a governed but single-route model boundary. A single route
cannot express tenant/provider segregation, repository data residency, or
dependency failover. Naive retry is also unsafe: it can multiply spend, send
source to an unapproved provider, and retry permanent credential errors.

## Decision

Add a versioned TOML route topology. Each route has a stable id, priority,
provider/model, HTTPS endpoint, API-key environment reference, optional exact
tenant selectors, repository glob selectors, region tag, and token pricing.
The file is trusted operator configuration and may not contain inline keys.

The repository policy gains `llm_region`. At intake it must have at least one
configured route satisfying provider, model, and region constraints. The
versioned task snapshot is propagated into `ModelRequest`, where the gateway
repeats those checks together with tenant/repository selectors.

Each route owns an independent circuit breaker. A configurable fallback budget
limits additional routes after the primary. Fallback is allowed for classified
transport/transient HTTP errors, open circuits, invalid structured output, or a
route-specific budget rejection. Permanent HTTP 4xx errors and internal
accounting failures stop immediately.

Schema version 7 adds root request id, route id, and attempt number to each
ledger record so operators can reconstruct one logical request across routes.

## Consequences

- One provider outage does not automatically open unrelated provider circuits.
- Repository policy cannot be bypassed by fallback.
- `fallback_attempts=0` gives strict single-call behavior; every extra attempt
  has its own atomic budget reservation and usage record.
- This decision originally kept route priority deterministic and left weighted
  routing/candidate shadowing for future work. ADR 0021 adds those capabilities
  without changing the v1 topology semantics; capacity-aware routing remains
  future work.
- The gateway improves application resilience but cannot manufacture regional
  HA; endpoints, credentials, quotas, and network paths must be independent in
  the deployment.
