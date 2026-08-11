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
floor, package build, dependency audit, secret scanning, and CodeQL.

The core coverage profile temporarily excludes the HTTP adapter, PostgreSQL adapter, module entry
point, and isolated subprocess runner. These boundaries require dedicated integration environments;
their omission is explicit instead of being hidden by a misleading repository-wide percentage.
Phase two should add API/PostgreSQL/container integration suites and then remove the exclusions.

## Consequences

- Unsafe or unverified self-evolution changes cannot merge silently.
- Dependency resolution is deterministic and auditable.
- Contributors get the same commands locally and in CI.
- Formatting creates a one-time broad diff, after which changes remain stable.
- Boundary integration coverage remains a documented follow-up rather than an implied guarantee.

