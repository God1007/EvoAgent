# ADR 0003: Use checksummed forward-only database migrations

- Status: Accepted
- Date: 2026-08-17

## Context

SQLite and PostgreSQL adapters previously executed a long list of `CREATE TABLE
IF NOT EXISTS` and conditional `ALTER TABLE` statements in their constructors.
This made fresh startup convenient but did not record which schema transitions
had occurred, could not detect a database created by a newer binary, and made
backend drift easy to miss. Rolling deployments also lacked one explicit lock
and compatibility decision.

## Decision

EvoAgent maintains an immutable, integer-versioned migration catalog with one
logical history and dialect-specific SQL. Each applied row records its version,
name, SHA-256 checksum, and timestamp.

- SQLite migrations execute under `BEGIN IMMEDIATE`; PostgreSQL uses a
  transaction advisory lock.
- Histories must be contiguous and checksums must match the application.
- A database schema newer than the binary is rejected at startup.
- Existing unversioned databases are adopted through idempotent migrations
  rather than rebuilt.
- Migrations are forward-only. Incompatible rollback restores a verified
  pre-migration backup instead of inventing reverse DDL.
- Schema evolution should use expand/migrate/contract across releases.
- A dedicated `evoagent-migrate` entry point supports deployment jobs, while
  Store startup retains the same safety check as a final guard.

The built-in runner avoids adding a framework dependency and supports the two
existing adapters through the same catalog. If migration complexity later
requires online backfills or a dedicated framework, that tooling must preserve
this history and compatibility policy.

## Consequences

- Startup is fail-closed for newer, incomplete, or modified histories.
- Concurrent instances cannot race the same migration sequence.
- Fresh install, previous-version upgrade, legacy adoption, and rollback on
  injected failure are deterministic tests.
- Released migration definitions are immutable; corrections require a new
  version.
- Operators must own backup/restore validation as part of deployment. A
  successful migration is not itself proof that a backup is restorable.

## Alternatives rejected

- **Continue idempotent constructor DDL:** no transition history or downgrade
  compatibility signal.
- **Automatic down migrations:** reverse DDL can destroy data and is unsafe as
  an unattended application-startup behavior.
- **Require Alembic immediately:** mature, but introduces another runtime and
  packaging surface before the project needs its broader features. The current
  catalog can be bridged later without weakening the recorded invariants.
