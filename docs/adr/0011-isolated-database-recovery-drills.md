# ADR 0011: Isolated database recovery drills

- Status: Accepted
- Date: 2026-08-17

## Context

A successful `pg_dump` does not prove recoverability.
Corrupt artifacts, missing schema objects, stale migration history, broken
sequences, unavailable client tools, and undocumented operator steps commonly
remain hidden until an incident. Restoring over a source database during a test
would create unacceptable destructive risk.

## Decision

Add a standalone `evoagent-dr` operational boundary.

- PostgreSQL backup and source fingerprints share one exported MVCC snapshot.
- PostgreSQL restore targets are generated internally and must match a strict
  `evoagent_drill_<uuid>` policy before create or drop operations are allowed.
- A pass requires immutable migration history, schema metadata, table counts,
  content fingerprints, database integrity, application adapter reads/writes,
  RPO/RTO objectives, and successful disposable-target cleanup.
- Artifacts and versioned JSON manifests are created with owner-only mode.
- Passwords never enter subprocess arguments or evidence; PostgreSQL tools
  receive only PostgreSQL and basic runtime environment variables, not
  application, GitHub, or model secrets.
- CI performs the real PostgreSQL dump/restore path and retains only manifests,
  not data-bearing dumps.

## Consequences

Recoverability is now executable and release evidence can be consumed without
parsing logs. The drill requires database-create permission and PostgreSQL
client binaries in a dedicated administrative job; those privileges and tools
do not belong in the serving container.

The row fingerprint is order-independent and bounded-memory. It is evidence of
logical equivalence, while the artifact SHA-256 protects artifact identity. The
manifest still depends on external immutable storage and access controls for
tamper resistance.

Redis rehydration is handled separately by the PostgreSQL-to-Redis recovery
command. Point-in-time recovery, cross-region failover and backup durability
remain deployment responsibilities.
