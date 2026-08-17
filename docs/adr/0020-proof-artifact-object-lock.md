# ADR 0020: Separate Proof artifacts and support verified S3 Object Lock

- Status: Accepted
- Date: 2026-08-17

## Context

The remote Proof Runner originally wrote canonical inputs and outcomes to an
append-only local directory. Content addressing detected a different payload at
the same digest path, but filesystem administrators could still delete the
volume and a single node could not provide regulated or multi-region retention.
Replacing the whole executor to change retention would couple two independent
trust decisions: where untrusted code runs and where its evidence is retained.

S3 Object Lock is version-scoped. A protected version does not prevent a new
version from being created at the same key, and governance retention can be
bypassed by a specially authorized principal. Merely calling `PutObject` or
checking a bucket flag is therefore insufficient evidence of immutable storage.

## Decision

- Define `ProofArtifactStorePort` with content-addressed put, backend identity,
  readiness, and lifecycle contracts. `ProofRunnerServer` depends only on this
  Port and includes required artifact health in `/readyz`.
- Move the existing local adapter behind the Port. It retains exclusive create,
  fsync, hard-link publication, no-follow reads, collision checking, and bounded
  `sha256:` references, but does not claim WORM durability.
- Add an S3 adapter for a deployment-controlled bucket with Versioning and Object
  Lock. It derives keys only from namespace plus SHA-256, writes with
  `If-None-Match: *`, supplies a full-object SHA-256 and explicit retention, and
  requires a returned version ID.
- Bounded-retry S3's concurrent-operation `409`; treat a conditional `412` as
  deduplication only after verifying the existing current version. Read back
  the exact version with checksums enabled and verify content length,
  digest metadata, checksum, retention mode, and retain-until date. A conditional
  conflict is treated as deduplication only after the existing current version
  passes the same checks.
- If identical content is reused later and its retention ends too soon, extend
  and re-read the exact version. A concurrent longer extension is accepted after
  read-back; a shorter extension is never requested with governance bypass and
  is rejected by Object Lock. `COMPLIANCE` is the default; `GOVERNANCE` remains
  explicit.
- Keep bucket, endpoint, and KMS configuration out of attestations/readiness;
  credential-capable endpoint and KMS fields are excluded from settings repr.
  Endpoints require credential-free HTTPS except loopback test services.
- Make boto3 a hash-locked runtime dependency so the supported production
  adapter is reproducible in the wheel and container, not an undeclared plugin.

## Consequences

Execution placement, replay control, and evidence retention are independently
replaceable. A storage failure with `REQUIRE_ARTIFACTS=true` produces
infrastructure uncertainty; failure to retain the canonical input occurs before
untrusted repository execution. Object references remain content hashes and do
not disclose account or bucket topology.

The adapter proves the request and read-back invariants in process. It does not
prove IAM least privilege, bucket-policy correctness, account survival,
replication, restore procedures, or behavior after retention expires. Operators
must independently audit those controls and exercise a real write/inspect/
restore path in every production account and region. A microVM execution
provider remains a separate hardening item.
