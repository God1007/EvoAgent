# ADR 0014: Reviewer seams and snapshotted Dynamic Skills

- Status: accepted
- Date: 2026-08-17
- Updated: 2026-08-21

2026-08-27 amendment: the fixed-coordinator restriction below is superseded by
the explicit requirement for composable agent workflows. See
[Agent workflows](../agent-workflows.md). Trusted agent stages now expose versioned
ports and pinned implementations; the stdlib DAG runner uses existing durable
checkpoints for handoffs. This does not add plugin discovery, hot loading, or
in-process execution of untrusted Skills; the snapshot/isolation decisions remain.

## Context

Reviewer and fix-rule extension points are useful; a general in-process plugin
runtime is not. Dynamic Skills also need to execute the source that was
validated rather than a later mutable file.

## Decision

Built-in reviewers and fix rules are composed directly. `ReviewerContribution`
and `FixRulePort` remain the narrow trusted seams. Loading validates a complete
Skill set before activation, bounds manifests before JSON parsing, snapshots
UTF-8 source by SHA-256, and rechecks the
digest in the bounded subprocess/container runner. Stdout/stderr are capped while
the process is running, the whole POSIX process group is terminated, and interrupted
containers are force-removed. Skill findings are count-bounded
and type-validated before they enter the shared coordinator. Process-local reload is
allowed only with the ephemeral development queue. Durable deployments load an
immutable Skill set at startup so a shared Redis consumer group cannot mix
reviewer revisions across replicas.
Every dynamic manifest declares protocol version `1`; both loading and the
isolated runner reject other versions, and the version participates in the
execution revision.
The parent accepts only the exact v1 finding fields, canonical severities and
fingerprints, bounded non-empty text, and evidence located on an added line
before a finding can enter collaboration storage.

## Consequences

The runtime has no plugin discovery, profiles, dependency graph or lifecycle
registry. Untrusted review logic remains isolated and content-addressed. A
production Skill change is an application deployment, not an API mutation.
