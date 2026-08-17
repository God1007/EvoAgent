# ADR 0019: Share Proof replay claims and bind rotating keys

- Status: Accepted
- Date: 2026-08-17

## Context

The authenticated Proof Runner originally kept accepted request UUIDs in one
process and knew one HMAC key. Two replicas could therefore accept the same
signed request, while changing the key required a synchronized stop-the-world
rollout. Sticky routing does not prevent a replay directed at another replica,
and an accept-all rotation window would invalidate the attestation boundary.

## Decision

- Define `ProofReplayStorePort` with atomic `claim`, readiness, backend identity,
  and lifecycle operations. The bounded in-memory adapter remains the explicit
  single-replica development topology.
- Add a Redis adapter that stores only a namespaced UUID nonce. It claims with
  `SET NX` and a TTL that covers the inclusive signed timestamp window. Redis
  errors become Runner-unavailable before repository execution begins; a false
  claim is a replay. Production can require shared replay at startup.
- Keep `/healthz` as liveness and add `/readyz` for the replay dependency. The
  API's remote executor health check uses readiness, so an unavailable shared
  guard removes the proof dependency from service readiness.
- Add a strict 1–64-character key ID to request/response headers and signed JSON
  bodies. The Runner has one current key and at most one previous key. It signs
  the response with the key selected and verified for that request; clients
  require the returned ID to match and include it in the attestation.
- Preserve protocol-v1 rolling compatibility for the literal `default` ID: a
  missing ID can select only key ID `default`, never an arbitrary named current
  key. A named-key client fails against an old Runner instead of downgrading.
  Deploy Runner overlap support, switch clients, wait for request/replay drain,
  then retire the previous key.
- Do not put signing keys or credential-bearing Redis URLs in readiness,
  attestations, logs, or dataclass representations.

## Consequences

Multiple Runner replicas now enforce one replay decision and can rotate HMAC
material without accepting unsigned work or invalidating in-flight clients.
Unit tests share one Redis-like atomic state across two services, inject replay
dependency failure, tamper with key IDs, and exercise current/previous/removal.
The mandatory Redis CI job proves real `SET NX` atomicity and positive TTL.

Redis is now part of the production proof control plane. Its outage deliberately
makes proofs inconclusive, not failed reproductions, and operators must not fall
back to memory during an incident. The adapter does not itself prove regional
Redis durability. A microVM executor and WORM evidence store remain separate
hardening requirements.
