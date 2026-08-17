# ADR 0014: Capability seams and snapshotted Dynamic Skills

- Status: Accepted
- Date: 2026-08-17

## Context

The trusted plugin runtime already provided declared capabilities, dependency
ordering, reversible effects, profiles, scopes, and transactional startup. The
review engine still constructed its built-in security and reliability reviewers
internally, so adding one reviewer required replacing the whole engine. Dynamic
Skills were hash-checked during reload but executed later from the mutable source
path, leaving a check-to-use race and non-transactional removal behavior.

[DeepSeek Harness](https://github.com/God1007/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/docs/architecture.md)
demonstrates two relevant patterns: a replaceable capability is designed as a
Service Definition, one or more Providers, and a Consumer; plugin registrations
are lifecycle effects that unwind on unload. EvoAgent adopts those patterns
selectively rather than making policy and security invariants replaceable by
untrusted code.

## Decision

- `ReviewerContribution` is the stable service-definition object. It carries a
  reviewer plus versioned, auditable metadata.
- `review.reviewer` is multi-valued. Built-in security and reliability reviewers
  are independent trusted Provider plugins, while `ReviewEngine` is the Consumer.
- Provider priority defines deterministic contribution order. Duplicate
  contribution IDs fail candidate-graph startup and trigger full rollback.
- Trusted entry-point plugins remain explicit allowlist-only, in-process code.
  Untrusted review logic remains a Dynamic Skill and cannot provide trusted
  capabilities.
- Dynamic reload validates a complete candidate set before swapping it under one
  lock. Failed reloads retain the previous set; removed Skills retire only after a
  successful commit; trusted/dynamic name collisions are rejected.
- A Skill executes the exact UTF-8 source snapshot whose SHA-256 was validated at
  reload. The isolated runner verifies that digest again before `compile`/`exec`.
- Source, output, error, time, memory, file size, and file-descriptor use are
  bounded. File writes, network, subprocess, process-control, and dangerous OS
  audit events are blocked. Deployments can require a digest-pinned container;
  missing container configuration then fails closed.

## Consequences

An enterprise reviewer can now be installed, ordered, disabled, or removed
without editing the coordinator or replacing `review.engine`. Runtime inventory
shows the exact Provider set. Dynamic Skill changes behave as versioned
activations instead of mutable-path execution.

EvoAgent does not adopt unrestricted production HMR or “everything is a plugin.”
Authentication, policy enforcement, verification, state transitions, and trust
classification remain privileged invariants. Live graph replacement and
layered/bundled profile overlays may be added later only through candidate graph
construction, health checks, and atomic traffic handoff.
