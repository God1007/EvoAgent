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

Matching rule: same path, same CWE, predicted line within the labeled range or
within 2 lines. Duplicate predictions match once; the rest count as FP.

## 3. Splits and gating

- Validation and Holdout are split **by repository** to avoid leakage.
- A candidate activates only if the quantitative gate passes **and** protected
  metrics do not regress beyond tolerance on both splits.
- The `production_data_provenance` gate intentionally **fails** on the synthetic
  corpus: production activation requires a real, independently-labeled public-PR
  dataset. This is by design, not a bug.

## 4. Honesty boundary

The built-in 100-case corpus is a **controlled synthetic benchmark** used to
validate the evaluation code and metric definitions. It must not be presented as
production performance on real public PRs. Report real and synthetic numbers
separately, always with dataset, sample count, split method, and version.

## 5. What to capture per release

The baseline snapshot records: version + commit SHA, dataset SHA-256, Python and
dependency versions, the metric table, per-split results, and the release-gate
verdict. Regenerate it with the commands above and update
[`evaluation-baseline.md`](evaluation-baseline.md).
