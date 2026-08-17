# EvoAgent Baseline Evaluation Report

Reproducible snapshot of the end-to-end evaluation harness. Regenerate with
`python scripts/run_e2e_evaluation.py --reuse-dataset` (see
[`evaluation.md`](evaluation.md)).

## Provenance

| Field | Value |
| --- | --- |
| EvoAgent version | 0.18.0 |
| Python | 3.12 |
| Dependencies | pinned via `requirements.lock` (hash-locked) |
| Dataset | `evaluation_data/pr_diff_100.jsonl` |
| Dataset semantic SHA-256 | `844b6d45c3de39c6c1d8080067bb77900a7234a68891c264f5833b6abdf6e770` |
| Corpus kind | `synthetic-controlled` (not real production PRs) |
| Evaluation report schema | 2 |

## Dataset

- 100 PR diffs (40 risk, 60 clean) across 10 repositories.
- Validation/Holdout split by repository to avoid leakage.

## Overall results

| Metric | Single-agent baseline | Multi-agent candidate | Δ |
| --- | ---: | ---: | ---: |
| Precision | 84.4% | 82.5% | -1.9 pp |
| Recall | 67.5% | 82.5% | +15.0 pp |
| F1 | 75.0% | 82.5% | +7.5 pp |
| Severity accuracy | 96.3% | 97.0% | +0.7 pp |
| High-risk recall | 89.5% | 94.7% | +5.3 pp |
| Clean PR accuracy | 91.7% | 91.7% | +0.0 pp |
| Execution success | 100.0% | 100.0% | +0.0 pp |
| Safe-fix rate | — | 100.0% | — |
| E2E security-fix rate | — | 70.0% | — |

Counts: baseline TP/FP/FN = 27/5/13; candidate TP/FP/FN = 33/7/7.
Auto-repair: all 28 deterministic-whitelist-eligible risks passed all five
repair gates; 5 detected but ambiguous/non-whitelisted risks safely abstained;
28 of 40 total risk cases succeeded end-to-end.

## Per-split results

| Split | Cases | Risk/Clean | F1 | High-risk recall | Clean accuracy |
| --- | ---: | ---: | ---: | ---: | ---: |
| Validation | 80 | 32/48 | 83.6% | 100.0% | 89.6% |
| Holdout | 20 | 8/12 | 76.9% | 66.7% | 100.0% |

## Slices and confidence calibration

- Language slice: all 100 controlled cases are Python, so Python F1 is 82.5%.
  This corpus provides no cross-language performance evidence.
- The report now emits per-CWE expected/predicted/TP/FP/FN metrics and per-rule
  precision. Important visible weaknesses include zero recall on CWE-117,
  CWE-362, CWE-367, CWE-400, CWE-601, and CWE-682; CWE-798 precision is 44.4%.
- All 40 reported finding confidences are structurally valid. Finding-correctness
  ECE is 11.7% and Brier score is 0.1571. All predictions currently land in the
  0.9–1.0 bin (mean confidence 94.2%, observed precision 82.5%), so the candidate
  is measurably over-confident even though its aggregate F1 improves.

## Release gate

- Quantitative gate: **PASS**
- Production activation: **BLOCKED** — requires a real, independently-labeled
  public-PR dataset (`production_data_provenance` fails by design on synthetic
  data).

| Gate | Result |
| --- | --- |
| `validation_f1_improvement` | PASS |
| `same_dataset` | PASS |
| `high_risk_recall_non_regression` | PASS |
| `clean_accuracy_non_regression` | PASS |
| `holdout_f1_non_regression` | PASS |
| `execution_success` | PASS |
| `confidence_validity` | PASS |
| `safe_fix_rate` | PASS |
| `e2e_security_fix_rate` | PASS |
| `production_data_provenance` | FAIL (expected on synthetic corpus) |

The provenance audit passes sample size, unique content, repository-disjoint
splits, and holdout coverage. It intentionally fails immutable public-source,
usage-rights, independent-blind-annotation, single-protocol, and sidecar-binding
checks. These failures prevent synthetic results from being promoted by merely
changing a source label.
