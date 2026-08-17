# ADR 0021: GitOps model-route promotion with isolated shadow traffic

- Status: accepted
- Date: 2026-08-17

## Context

Priority-only routing cannot split capacity between equivalent active endpoints,
and changing a production model without live operational evidence is unsafe.
Running a candidate inline would add its latency and failures to the production
request. Persisting prompts for a durable shadow queue would create a new source
code and secret-retention surface. Treating agreement with the current model as
quality would also preserve the current model's defects.

## Decision

Version 2 of the trusted route topology gives each route an explicit `active`,
`candidate`, or `disabled` state. Active routes in the same priority tier use
deterministic weighted sampling; priority tiers still define the fallback order.
Version 1 retains its original ordered behavior.

A candidate names one active baseline and a deterministic shadow percentage.
Only after the active call succeeds may the gateway schedule the candidate. A
bounded in-process executor keeps the redacted prompt in memory, returns the
active response without waiting, and drains through the plugin lifecycle. The
candidate cannot enter the active/fallback catalog, affect readiness, or replace
the active result. Candidate calls use the same total budget plus an optional
shadow-only budget ceiling.

Before scheduling, the gateway durably records a `scheduled` observation. It
stores request/output hashes, route and tenant scope, token/cost/duration data,
and message-free error fingerprints, never prompt or response content. A crash
therefore leaves visible pending evidence and any started provider reservation
becomes `uncertain`; neither is silently counted as success.

The promotion report requires enough successful samples, bounded candidate
error/disagreement rates, no pending observations, and SHA-256 references to an
independent offline dataset and evaluation report. Agreement is an operational
compatibility signal, not a quality score. The service never mutates routing
state automatically: an operator must review the report, change the topology in
version control, and redeploy a new topology hash.

## Consequences

- Candidate latency and failure cannot fail or delay the completed active call.
- Weighted assignment is stable for the same task/request and avoids a mutable
  routing session store; weights apply only within one priority tier.
- Shadow observations are durable, but prompts are deliberately not queued.
  A process crash may lose an unstarted in-memory candidate call; its scheduled
  row blocks promotion until the reservation TTL classifies it as a terminal
  uncertain/error sample (or a new topology deliberately supersedes it).
- A candidate cannot self-promote from agreement alone, and topology activation
  remains reviewable and rollbackable through normal deployment controls.
- Capacity-aware routing and automatic weight adjustment remain out of scope.
