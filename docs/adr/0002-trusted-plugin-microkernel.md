# ADR 0002: Compose application capabilities through a trusted plugin microkernel

- Status: Accepted
- Date: 2026-08-17

## Context

EvoAgent originally assembled storage, queues, model adapters, the reviewer graph,
GitHub delivery, repair, authentication, rollout, alerting, and evolution directly
inside `ReviewService.__init__`. The implementations were individually testable,
but replacing one capability required editing the application service. Resource
ownership and startup failure semantics were also implicit.

The project already has a dynamic Skill mechanism. Skills are untrusted review
extensions and therefore run out of process. Infrastructure and workflow
providers need a different model: they are installed by an operator, execute in
process, and may legitimately own database pools or subscribe to lifecycle
events. Treating these two trust levels as one plugin system would either expose
credentials to untrusted code or make normal infrastructure integration unusable.

## Decision

EvoAgent adopts a small trusted plugin runtime with these contracts:

1. `CapabilityKey[T]` is the stable seam between a provider and a consumer.
2. `PluginManifest` declares plugin id, semantic version, plugin API version,
   provided capabilities, required capabilities, optional dependencies, and
   priority before activation.
3. The runtime validates the selected graph, rejects missing dependencies and
   cycles, then activates it in topological order.
4. Every registration, event subscription, and resource cleanup is a reversible
   effect. If any plugin fails to start, the complete candidate graph is unwound
   in reverse order. Normal shutdown uses the same reverse dependency order.
5. A TOML `PluginProfile` can select/disable plugins and provide per-plugin
   configuration. Installed entry-point discovery is off by default and requires
   an explicit operator allowlist.
6. Child runtimes support tenant/repository/session composition. A child can
   shadow a parent capability without mutating the parent. Scope is lifecycle
   and configuration isolation, not a security sandbox.
7. Lifecycle/review events are observer-only in this phase. Listener failures
   are isolated and counted; they cannot fail the review path.
8. Deterministic repair rules are multi-valued `fix.rule` capabilities. Each rule
   is independently replaceable or disableable while `SafeFixer` continues to
   own compilation/test verification and publication safety.

The stable plugin API is `evoagent.plugin/v1`. Capability definitions live in
`evoagent/capabilities.py`; the default provider catalog and composition root live
in `evoagent/bootstrap.py`. The default review graph is isolated behind the
`review.engine` capability.

## Invariants kept in the kernel

- Domain schemas (`Finding`, `ReviewReport`, task states and stable fingerprints).
- Task durability, checkpoint semantics, tenant authorization and audit records.
- Plugin graph validation and lifecycle rollback.
- Verification gates and the distinction between a proposed fix and a published fix.
- The sandbox boundary for untrusted Skills and untrusted repository execution.

These invariants are intentionally not arbitrary event middleware. An extension
cannot silently redefine task success, bypass authorization, or mark an
unverified patch as verified.

## Trust policy

Trusted plugins run in the service process and can access anything available to
that process. They must be reviewed, pinned, allowlisted, and covered by the same
supply-chain controls as application dependencies. Dynamic Skills remain the
only supported mechanism for untrusted reviewer code and receive no host
credentials.

## Consequences

- New storage, queue, model, code-host, workflow, telemetry, and repair-rule
  providers can be integrated without editing `ReviewService`.
- Startup failures no longer leave a partially assembled service or leaked pool.
- The `/health` response exposes the active profile and plugin runtime state.
- The project gains more abstractions and graph-level tests. Capability names and
  plugin API versions now require compatibility discipline.
- Hot replacement of a live global runtime is not implemented yet. A new graph
  is built transactionally, but process-level rollout remains the production
  mechanism until in-flight task draining and atomic handover are specified.

## Alternatives rejected

- **Keep constructor wiring:** simple initially, but every new provider expands
  the central service and makes ownership harder to verify.
- **Make everything a plugin:** too much indirection for a focused PR-review
  system and would weaken kernel invariants.
- **Reuse dynamic Skills for infrastructure:** violates the trust model because
  infrastructure providers need credentials and long-lived resources.
- **Load every installed entry point automatically:** creates an unacceptable
  supply-chain activation path. Discovery therefore requires opt-in plus an
  explicit allowlist.
