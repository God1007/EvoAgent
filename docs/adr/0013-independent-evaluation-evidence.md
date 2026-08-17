# ADR 0013: Bind production quality claims to independent evaluation evidence

- Status: Accepted
- Date: 2026-08-17

## Context

The deterministic 100-case corpus proves evaluator behavior but is generated in
the same project as the reviewers. A `public-github-pr` string alone does not
prove that content is immutable, legally approved for this use, independently
labelled, blind to system output, or isolated from the holdout repositories.
Aggregate F1 also hides language/rule failures and over-confident predictions.

## Decision

Separate public input acquisition, annotation, adjudication, scoring, and
activation evidence.

- The public-PR importer emits answer-free cases and records the GitHub URL,
  stable head SHA, Diff SHA-256, retrieval time, and human-approved usage-rights
  plus sensitive-data review reference. It rechecks the head around the bounded
  Diff fetch.
- At least two distinct opaque reviewers independently annotate each case while
  blind to system output. One different reviewer adjudicates final truth.
- Annotation packets contain only structured location/CWE/severity labels. The
  compiler validates labels against added lines, rejects identity/protocol/split
  conflicts, computes positive-pair and clean-pair agreement separately, excludes
  clean pairs from matched-finding severity agreement, and emits canonical dataset
  and sidecar hashes.
- Production provenance requires a matching sidecar, repository-disjoint
  validation/holdout sets, holdout/risk/clean coverage, unique content, immutable
  public sources, approved rights, one protocol, and at least 50 cases.
- Baseline and candidate reports must use the same semantic dataset hash.
- Reports include language, CWE, rule, finding-confidence ECE/Brier, and invalid
  confidence slices. Synthetic and independently labelled results remain
  separate.

## Consequences

Changing dataset metadata cannot by itself enable production activation. Raw
annotation packets can stay in a restricted data domain while the scored corpus
retains pseudonymous reviewer IDs and packet hashes. Rights approval remains a
human/legal assertion that the software verifies and binds; the gate does not
replace legal review.

This establishes the evidence protocol but does not manufacture independent
ground truth. Production activation remains blocked until an approved public PR
sample is actually acquired, annotated, adjudicated, retained, and evaluated.
