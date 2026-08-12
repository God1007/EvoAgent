# EvoAgent Baseline Evaluation Report

Reproducible snapshot of the end-to-end evaluation harness. Regenerate with
`python scripts/run_e2e_evaluation.py --reuse-dataset` (see
[`evaluation.md`](evaluation.md)).

## Provenance

| Field | Value |
| --- | --- |
| EvoAgent version | 0.3.0 |
| Python | 3.12 |
| Dependencies | pinned via `requirements.lock` (hash-locked) |
| Dataset | `evaluation_data/pr_diff_100.jsonl` |
| Dataset SHA-256 | `88831bb19264f9fc15433de7801b623aad38b80076f5d5b085d0299fd40cc115` |
| Corpus kind | `synthetic-controlled` (not real production PRs) |

## Dataset

- 100 PR diffs (40 risk, 60 clean) across 10 repositories.
- Validation/Holdout split by repository to avoid leakage.

## Overall results

| Metric | Single-agent baseline | Multi-agent candidate | Δ |
| --- | ---: | ---: | ---: |
| Precision | 83.3% | 82.5% | -0.8 pp |
| Recall | 62.5% | 82.5% | +20.0 pp |
| F1 | 71.4% | 82.5% | +11.1 pp |
| Severity accuracy | 100.0% | 100.0% | +0.0 pp |
| High-risk recall | 84.2% | 94.7% | +10.5 pp |
| Clean PR accuracy | 91.7% | 91.7% | +0.0 pp |
| Execution success | 100.0% | 100.0% | +0.0 pp |
| Safe-fix rate | — | 78.8% | — |
| E2E security-fix rate | — | 65.0% | — |

Counts: baseline TP/FP/FN = 25/5/15; candidate TP/FP/FN = 33/7/7.
Auto-repair: 26 of 33 matched risks passed all five repair gates; 26 of 40 total
risk cases succeeded end-to-end.

## Per-split results

| Split | Cases | Risk/Clean | F1 | High-risk recall | Clean accuracy |
| --- | ---: | ---: | ---: | ---: | ---: |
| Validation | 80 | 32/48 | 83.6% | 100.0% | 89.6% |
| Holdout | 20 | 8/12 | 76.9% | 66.7% | 100.0% |

## Release gate

- Quantitative gate: **PASS**
- Production activation: **BLOCKED** — requires a real, independently-labeled
  public-PR dataset (`production_data_provenance` fails by design on synthetic
  data).

| Gate | Result |
| --- | --- |
| `validation_f1_improvement` | PASS |
| `high_risk_recall_non_regression` | PASS |
| `clean_accuracy_non_regression` | PASS |
| `holdout_f1_non_regression` | PASS |
| `execution_success` | PASS |
| `safe_fix_rate` | PASS |
| `e2e_security_fix_rate` | PASS |
| `production_data_provenance` | FAIL (expected on synthetic corpus) |
