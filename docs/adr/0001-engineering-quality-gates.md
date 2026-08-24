# ADR 0001: Enforce a reproducible engineering quality baseline

- Status: Accepted
- Date: 2026-08-11

## Context

EvoAgent can evolve prompts and review Skills, but an evolution system is only credible when its
own changes are reproducible, reviewable, and reversible. The repository previously had tests but
no unified project configuration, dependency lock, CI matrix, coverage gate, type gate, or
automated security checks.

## Decision

The repository uses `pyproject.toml` as the source of truth for package metadata and developer
tool configuration. Runtime and development environments are pinned in hash-verified lock files.
Every pull request must pass Ruff, mypy, the Python 3.11/3.12 test matrix, a 70% core line coverage
floor, package build, dependency audit, CycloneDX runtime inventory, secret scanning, and CodeQL.

The HTTP adapter is included in the core coverage floor after its server-boundary suite landed.
The PostgreSQL adapter, module entry point, and isolated subprocess runner remain explicitly
excluded from the aggregate percentage because their evidence comes from dedicated PostgreSQL,
Redis, wheel, and container contract jobs. Those jobs fail if a selected contract is skipped.

## Consequences

- Unsafe or unverified self-evolution changes cannot merge silently.
- Dependency resolution is deterministic and auditable.
- Third-party workflow Actions execute only SHA-pinned commits; Dependabot owns upgrades.
- Contributors get the same commands locally and in CI.
- Formatting creates a one-time broad diff, after which changes remain stable.
- Boundary evidence is enforced separately instead of inflating the core line percentage.

