# ADR 0009: Authenticated remote Proof Runner boundary

- Status: accepted
- Date: 2026-08-17

## Context

Proof commands and repository files are attacker-controlled. Running them from
the API/worker deployment means a container-runtime mistake can expose the same
host and network identity that hold Store, Redis, GitHub, and model credentials.
An infrastructure timeout must also never be interpreted as a reproduced bug.

## Decision

Introduce the stable `proof.executor` capability and `ProofExecutorPort`.
`RepairUseCases` owns the evidence ladder but delegates each command execution
to the selected provider. The built-in provider is either a fail-closed local
container executor or an authenticated remote adapter.

The remote protocol is versioned and uses canonical JSON. Every request has a
UUIDv4, timestamp, canonical input SHA-256, body SHA-256, and HMAC-SHA256. The
runner enforces a replay window and in-process nonce cache before execution.
Every response repeats the request/input identity and signs the response body;
the evidence digest is the SHA-256 of the canonical outcome. HTTP redirects are
disabled, response/request sizes are bounded, the destination uses an exact
host allowlist, and non-loopback traffic requires HTTPS. HTTP connection
threads, execution slots, and replay-cache entries are independently bounded.

The standalone `evoagent-proof-runner` process accepts only the runner protocol.
It requires a container image and creates `RepairVerifier` with
`require_container=True`. Jobs have no injected environment, no network,
read-only container roots, dropped Linux capabilities, `no-new-privileges`, and
CPU/memory/PID/file/output/time limits. Optional runner-local artifact storage
uses append-only content addresses; it can be required so persistence failures
fail closed.

The evidence ladder treats transport, capacity, authentication, timeout, and
runner errors as uncertainty. Only an actual non-zero reproduction result can
produce L2, and only a signed passing patched result can produce L3/L4.

## Consequences

- API/worker deployments no longer need access to a container runtime when a
  remote provider is configured.
- The runner must live in a dedicated trust domain with no application secrets;
  giving it a shared production Docker socket would weaken that isolation.
- Key IDs are bound inside request/response bodies. A Runner can accept exactly
  one previous key during a coordinated overlap and signs each response with
  the request-selected key; named-key clients fail closed against old Runners.
- Replay protection is a Port. Local mode uses a bounded process cache;
  production replicas use Redis atomic `SET NX` + TTL and expose dependency
  readiness separately from liveness.
- Filesystem content addressing detects mutation but is not object-lock/WORM.
  Regulated retention should replace `proof.executor` or extend the runner with
  an immutable object-store adapter.
- Container isolation is materially stronger than host execution but is not a
  microVM boundary. High-risk multi-tenant deployments should use a compatible
  Firecracker/Kata provider behind `ProofExecutorPort`.
