# ADR 0007: Governed single-route model gateway

- Status: accepted
- Date: 2026-08-17
- Updated: 2026-08-20

## Context

Reviewers must not own arbitrary URLs, credentials or transport behavior.

## Decision

All model calls cross `ModelGatewayPort`. The built-in gateway accepts exactly
one route, loads its credential from the environment, redacts input, requires
HTTPS and an exact host allowlist, applies repository provider/model/region
policy, bounds input/output/response size, validates structured output and uses
a local circuit breaker.

## Consequences

There is one auditable egress boundary. Weighted routing, fallback, shadow
traffic, distributed capacity and cost settlement are absent until a measured
requirement justifies them.
