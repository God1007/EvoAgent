# ADR 0014: Reviewer seams and snapshotted Dynamic Skills

- Status: accepted
- Date: 2026-08-17
- Updated: 2026-08-20

## Context

Reviewer and fix-rule extension points are useful; a general in-process plugin
runtime is not. Dynamic Skills also need to execute the source that was
validated rather than a later mutable file.

## Decision

Built-in reviewers and fix rules are composed directly. `ReviewerContribution`
and `FixRulePort` remain the narrow trusted seams. Dynamic reload validates a
complete Skill set before swapping it, snapshots UTF-8 source by SHA-256, and
rechecks the digest in the bounded subprocess/container runner.

## Consequences

The runtime has no plugin discovery, profiles, dependency graph or lifecycle
registry. Untrusted review logic remains isolated and content-addressed.
