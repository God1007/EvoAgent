# ADR 0005: Versioned repository policy as a replaceable capability

- Status: accepted
- Date: 2026-08-17

## Context

The original `repository_grants` table answered only two questions: whether a
tenant could access a repository and whether auto-fix was enabled. Reviewer,
model, Diff-size, comment-publication, and FixRule choices were global process
configuration. That is insufficient for enterprise tenants with repositories
of different sensitivity, and ad-hoc checks inside `ReviewService` would couple
governance to orchestration.

## Decision

Introduce `RepositoryPolicyResolver` behind the stable `policy.repository`
capability and a narrow `RepositoryPolicyStorePort`. Schema version 5 stores one
current normalized policy plus immutable actor-attributed versions. Updating a
policy and its audit record is one database transaction.

Application use cases resolve one immutable `RepositoryPolicy` at intake. The
task persists its versioned snapshot for retry determinism. Current `enabled`
and comment-publication restrictions are also checked during queued execution;
shadow model egress rechecks `enabled` after the primary review, so emergency
revocation takes effect without mutating accepted history.

The legacy grant behavior remains a compatibility fallback only when an exact
versioned policy does not exist. The first explicit policy becomes authoritative
for that tenant/repository key.

## Consequences

- Governance decisions are testable without HTTP or `ReviewService`.
- Tenant policy changes are attributable, replayable, and race-safe.
- Fix rules remain composable but a repository can restrict the eligible subset.
- Model cost, quota, and data-residency fields are deferred until a gateway can
  enforce them; the current schema does not pretend otherwise.
- Policy tables and Store methods are part of the PostgreSQL contract and
  migration history.
