# ADR 0015: Compose bounded, content-addressed plugin Profiles

- Status: Accepted
- Date: 2026-08-17

## Context

A single plugin Profile cannot express the common enterprise deployment chain
of organization defaults, region policy, environment policy, and a release
override without copying a complete file. Copies drift, obscure the source of a
decision, and make rollback comparison difficult. Unbounded or implicit deep
merging, however, makes the effective plugin graph hard to predict and audit.

DeepSeek Harness uses Profiles and Bundles as explicit composition units. The
useful property for EvoAgent is deterministic, operator-selected composition;
production hot module replacement is not required and would need a separate
health-checked traffic handoff design.

## Decision

- `EVOAGENT_PLUGIN_PROFILE` names the base layer and
  `EVOAGENT_PLUGIN_PROFILE_LAYERS` names ordered overlays. Layers apply from left
  to right and the last explicit selection wins.
- Declaring `[profile].enabled` resets earlier enable/disable decisions and
  establishes a new allowlist. An empty list restores enable-by-default.
- A later plugin `config` replaces the earlier plugin configuration as one
  value. There is no implicit deep merge.
- A stack is limited to 16 unique resolved paths and each UTF-8 TOML document is
  limited to 1 MiB. The schema rejects unknown fields, duplicate identifiers,
  invalid types, and contradictory selections before plugin activation.
- Effective configuration is recursively frozen. Providers receive independent
  copies so plugin mutation cannot change the Profile or another Provider's
  view.
- Each layer is represented in runtime inventory by basename plus source
  SHA-256. A separate SHA-256 binds the complete effective Profile, including
  ordered layer identities. Configuration values and secrets are never exposed
  by the inventory endpoint.
- Profiles are loaded only while constructing a candidate process graph.
  Production changes use deployment canaries and process rollback, not live HMR.

## Consequences

The same reviewed base graph can be safely specialized per region and
environment without file duplication. Operators can compare instance inventory
against a release manifest and distinguish source drift from an intentional
overlay. Whole-configuration replacement requires overlays to repeat all values
for a plugin, trading brevity for clear ownership and rollback semantics.

This decision does not introduce untrusted plugins, remote Profile fetching,
secret storage, or atomic live graph replacement. Those remain separate trust
and lifecycle problems.
