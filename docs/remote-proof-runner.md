# Remote Proof Runner

The remote runner moves execution of caller-provided commands and PR source out
of the API/worker trust domain. It is an optional production topology; local
development retains the same evidence ladder through a container-only local
executor.

## Trust topology

```text
API / worker trust domain                    isolated runner trust domain
┌──────────────────────────┐                ┌──────────────────────────────┐
│ RepairUseCases           │ signed HTTPS   │ evoagent-proof-runner        │
│   → ProofRunner          ├───────────────►│   replay + size gates        │
│   → proof.executor       │◄───────────────┤   content-addressed evidence │
│ no Docker access needed  │ signed result  │   → one netless job container│
└──────────────────────────┘                └──────────────────────────────┘
       Store / Redis / GitHub / LLM keys          no application credentials
```

For production, place the runner on dedicated nodes or a separate cluster
account. Do not mount a production control-plane Docker socket into the API
container. The runner needs access only to its job runtime, TLS/HMAC secret, and
optional artifact volume.

## Protocol guarantees

- canonical JSON protocol version 1;
- UUIDv4 request identity and bounded timestamp replay window;
- HMAC-SHA256 request and response signatures with a key of at least 32 bytes;
- request body, canonical input, and canonical evidence SHA-256 bindings;
- exact destination hostname allowlist, no redirects or environment proxies;
- HTTPS outside `127.0.0.1`, `::1`, or `localhost`;
- bounded request, source, file count, response, command output, runtime, CPU,
  memory, PID count, and file size;
- content-addressed input/evidence artifacts when storage is configured;
- runner/transport/capacity failures map to `error`, never `failed`, so they do
  not manufacture L2 evidence.

## Start a dedicated runner

The command intentionally refuses to start without a strong signing key and a
job container image:

```bash
export EVOAGENT_PROOF_RUNNER_HOST=127.0.0.1
export EVOAGENT_PROOF_RUNNER_PORT=8091
export EVOAGENT_PROOF_RUNNER_SIGNING_KEY='<at-least-32-random-bytes>'
export EVOAGENT_PROOF_RUNNER_CONTAINER_IMAGE='company/proof-job@sha256:<digest>'
export EVOAGENT_PROOF_RUNNER_ARTIFACT_DIR=/var/lib/evoagent-proof
export EVOAGENT_PROOF_RUNNER_REQUIRE_ARTIFACTS=true
evoagent-proof-runner
```

Binding outside loopback additionally requires
`EVOAGENT_PROOF_RUNNER_TLS_CERT_FILE` and
`EVOAGENT_PROOF_RUNNER_TLS_KEY_FILE`. Terminate TLS at the runner itself or keep
the process on loopback behind an authenticated service-mesh sidecar. Pin the
job image by digest and preinstall only the language/test tooling repositories
are permitted to use; jobs have network disabled and cannot download packages.

## Connect the API/worker deployment

```bash
export EVOAGENT_PROOF_RUNNER_URL='https://proof.internal.example/v1/execute'
export EVOAGENT_PROOF_RUNNER_ALLOWED_HOSTS='proof.internal.example'
export EVOAGENT_PROOF_RUNNER_SIGNING_KEY='<same-secret-from-secret-manager>'
export EVOAGENT_PROOF_REQUIRE_REMOTE=true
```

`EVOAGENT_PROOF_REQUIRE_REMOTE=true` makes incomplete configuration a startup
error. Without a URL, the built-in executor remains local but still sets
`require_container=True`; it never falls back to host execution for `/v1/proofs`.

## Evidence retention and audit

With an artifact directory, each step stores two immutable-by-construction JSON
objects:

```text
inputs/<sha-prefix>/<canonical-input-sha>.json
evidence/<sha-prefix>/<canonical-outcome-sha>.json
```

The API response includes both `sha256:` artifact addresses inside each step's
`attestation`. Identical artifacts deduplicate; existing content is compared and
never overwritten. Back up the volume or replace the executor/storage provider
with object storage using retention lock when organizational policy requires
durable/WORM evidence.

## Operational boundaries

- `/healthz` reports process liveness; it does not execute a job.
- When `EVOAGENT_PROOF_REQUIRE_REMOTE=true`, the API `/ready` check includes
  runner liveness and removes the instance from service if it is unavailable.
- Concurrency is fail-fast and bounded by
  `EVOAGENT_PROOF_RUNNER_MAX_CONCURRENCY`; HTTP connection threads and replay
  cache entries have separate hard caps.
- Rotate the HMAC key with a coordinated API/runner rollout; dual-key overlap is
  not implemented in protocol v1.
- The built-in replay cache is per process. Use one replica or an external
  routing/replay control until a shared nonce adapter is available.
- A container shares the host kernel. Use a `proof.executor` plugin backed by
  microVMs for hostile public multi-tenant workloads.

See [ADR 0009](adr/0009-authenticated-remote-proof-runner.md) and the
[threat model](threat-model.md) for the accepted trade-offs.
