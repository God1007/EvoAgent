# EvoAgent Evaluation

How the review quality is measured, how to reproduce the numbers, and how to
read them honestly. A captured snapshot lives in
[`evaluation-baseline.md`](evaluation-baseline.md).

## 1. Reproduce

```bash
# Regenerate the controlled corpus and run the full harness
python scripts/run_e2e_evaluation.py

# Or replay the committed dataset without regenerating it
python scripts/run_e2e_evaluation.py --reuse-dataset
```

Outputs (git-ignored under `output/evaluation/`):

- `evaluation-report.json` — machine-readable results, splits, and release gate.
- `evaluation-report.md` — the human-readable report.

The dataset is `evaluation_data/pr_diff_100.jsonl`; its SHA-256 is printed in the
report so any run is tied to an exact corpus.

### Independently labelled public-PR workflow

Do not put answers in the import manifest. First record a human-approved usage
basis, then fetch immutable PR inputs:

```json
{"repository":"owner/repo","pull_request":123,"split":"holdout","rights":{"usage_basis":"repository-license","spdx_id":"Apache-2.0","review_status":"approved","data_review_status":"approved","review_reference":"LEGAL-EVAL-42"}}
```

```bash
python scripts/import_github_pr_dataset.py \
  public-pr-manifest.jsonl output/evaluation/public-pr-inputs.jsonl --limit 100
```

For every case, collect at least two annotation packets and exactly one
adjudication packet. Reviewer IDs are opaque pseudonyms; annotations must contain
only structured labels and must be completed without seeing any system output:

```json
{"schema_version":1,"case_id":"owner/repo#123","role":"annotation","reviewer_id":"annotator-a","protocol_id":"secure-review-v1","blind_to_system_output":true,"findings":[{"path":"src/app.py","start_line":18,"end_line":18,"cwe":"CWE-89","severity":"high"}]}
{"schema_version":1,"case_id":"owner/repo#123","role":"annotation","reviewer_id":"annotator-b","protocol_id":"secure-review-v1","blind_to_system_output":true,"findings":[]}
{"schema_version":1,"case_id":"owner/repo#123","role":"adjudication","reviewer_id":"adjudicator-c","protocol_id":"secure-review-v1","blind_to_system_output":true,"findings":[{"path":"src/app.py","start_line":18,"end_line":18,"cwe":"CWE-89","severity":"high"}]}
```

Compile labels and bind the resulting dataset to its raw annotation packets:

```bash
evoagent-eval-labels \
  --cases output/evaluation/public-pr-inputs.jsonl \
  --annotations private/annotation-packets.jsonl \
  --output output/evaluation/public-pr-labelled.jsonl \
  --evidence output/evaluation/annotation-evidence.json

python scripts/run_e2e_evaluation.py \
  --reuse-dataset \
  --dataset output/evaluation/public-pr-labelled.jsonl \
  --annotation-evidence output/evaluation/annotation-evidence.json
```

The compiler rejects embedded answers, non-blind packets, duplicate reviewers,
an adjudicator who also annotated the case, mixed protocols, labels outside
added lines, duplicate labels, missing cases, and repository leakage between
validation and holdout. Output is canonical and reproducible; the sidecar stores
raw-case, annotation-bundle, and compiled-dataset SHA-256 values plus macro
finding F1, positive-pair finding F1, both-clean pair count, and matched-finding
severity agreement between annotators. Clean pairs do not enter the severity
denominator. Keeping positive and both-clean agreement separate
prevents a clean-heavy corpus from hiding weak issue-level agreement.

## 2. Metrics

| Metric | Definition |
| --- | --- |
| Precision | Reported findings that are real issues |
| Recall | Labeled issues that were found |
| F1 | Harmonic mean of precision and recall |
| High-risk Recall | Recall over Critical/High issues |
| Clean PR Accuracy | Clean PRs that produce **no** finding |
| Severity Accuracy | Correct severity on true positives only |
| Safe-fix Rate | Findings whose auto-fix passed all repair gates |
| E2E Security-fix Rate | Risk cases fixed end-to-end (reproduce → patch → compile → risk-cleared → regression) |
| Per-language | Full PR metrics grouped by languages inferred from changed-file extensions |
| Per-CWE | Expected/predicted/TP/FP/FN plus Precision, Recall, and F1 |
| Per-rule | Prediction count, TP/FP, Precision, and mean reported confidence |
| Finding ECE | Ten-bin expected calibration error for whether a reported finding is correct |
| Finding Brier | Mean squared error between finding confidence and TP/FP outcome |

Matching rule: same path, same CWE, predicted line within the labeled range or
within 2 lines. Duplicate predictions match once; the rest count as FP.

## 3. Splits and gating

- Validation and Holdout are split **by repository** to avoid leakage.
- A candidate is approved for rollout only if the quantitative gate passes
  **and** protected metrics do not regress beyond tolerance on both splits.
- Candidate Prompts are capped before persistence; expected findings use a closed,
  type-strict schema and must point to an added line.
- Baseline/candidate and validation/holdout replays share one
  `EVOAGENT_TIMEOUT_SECONDS` deadline; remaining cases become failed evaluations
  after expiry and cannot approve a partial run.
- Every qualification record binds the application/model/Skill execution revision
  alongside both Prompt hashes and both dataset hashes.
- Approval never changes production traffic; tenant canary/shadow rollout is the
  only activation path.
- Baseline and candidate reports must have the same semantic dataset SHA-256.
- Confidence metrics accept only finite JSON numbers in `[0,1]`; malformed values
  count as invalid and fail the release gate instead of being coerced or aborting the run.
- The `production_data_provenance` gate requires at least 50 unique cases,
  repository-disjoint validation/holdout splits, two or more holdout
  repositories, risk and clean cases in each split, immutable GitHub URL/head/diff
  evidence, approved usage-rights records, independent blind annotations under
  one protocol, and a matching compiler sidecar.
- The provenance gate intentionally **fails** on the synthetic corpus. Changing
  only `source.kind` cannot enable production activation.

## 4. Honesty boundary

The built-in 100-case corpus is a **controlled synthetic benchmark** used to
validate the evaluation code and metric definitions. It must not be presented as
production performance on real public PRs. Report real and synthetic numbers
separately, always with dataset, sample count, split method, version, annotation
agreement, and rights-review reference. An automated gate validates evidence
shape and cryptographic binding; it is not a substitute for legal or research
ethics review.

## 5. What to capture per release

Report schema v3 records the EvoAgent version, path-independent application
source SHA-256, exact Python version and `requirements.lock` SHA-256 alongside
the dataset SHA-256, metric table, per-split results and release-gate verdict.
Regenerate the baseline with the commands above and update
[`evaluation-baseline.md`](evaluation-baseline.md).
