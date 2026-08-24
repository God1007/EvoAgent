"""Generate/replay the 100-case benchmark and write JSON plus Markdown reports."""

import argparse
import hashlib
import json
import os
import platform
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent import __version__  # noqa: E402
from evoagent.evaluation_benchmark import (  # noqa: E402
    baseline_reviewer,
    candidate_reviewer,
    generate_controlled_pr_cases,
)
from evoagent.evaluation_dataset import load_jsonl  # noqa: E402
from evoagent.evaluation_harness import (  # noqa: E402
    EndToEndEvaluationHarness,
    FixtureRepairer,
    comparison_summary,
)
from evoagent.json_boundary import strict_json_loads  # noqa: E402
from evoagent.review_engine import _APPLICATION_SOURCE_REVISION  # noqa: E402


def write_jsonl(path, cases):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")


def percent(value):
    return "%.1f%%" % (100 * value)


def reproducibility_metadata():
    with open(os.path.join(ROOT, "requirements.lock"), "rb") as handle:
        lock_sha256 = hashlib.sha256(handle.read()).hexdigest()
    return {
        "evoagent_version": __version__,
        "python_version": platform.python_version(),
        "application_source_sha256": _APPLICATION_SOURCE_REVISION,
        "requirements_lock_sha256": lock_sha256,
    }


def markdown_report(baseline, candidate, comparison, reproducibility):
    b = baseline["metrics"]
    c = candidate["metrics"]
    dataset = candidate["dataset"]
    provenance = dataset.get("provenance") or {}
    source_note = (
        "> 注意：本次离线数据是受控合成基准，用于验证评测代码和计算口径，"
        "不能表述为真实公开 PR 的生产效果。"
        if dataset["source_kinds"] == ["synthetic-controlled"]
        else "> 生产数据证据门禁：%s；详见 JSON 报告中的逐项 provenance audit。"
        % ("PASS" if provenance.get("production_ready") else "BLOCKED")
    )
    lines = [
        "# EvoAgent 端到端 Evaluation Harness 报告",
        "",
        "## 数据集",
        "",
        "- 样本：%d 个 PR Diff（%d 风险，%d 干净）"
        % (dataset["cases"], dataset["risk_cases"], dataset["clean_cases"]),
        "- 仓库：%d 个，按仓库划分 Validation/Holdout" % dataset["repositories"],
        "- 来源标记：`%s`" % ", ".join(dataset["source_kinds"]),
        "- SHA-256：`%s`" % dataset["sha256"],
        "- EvoAgent：`%s`（源码 SHA-256：`%s`）"
        % (
            reproducibility["evoagent_version"],
            reproducibility["application_source_sha256"],
        ),
        "- Python：`%s`；requirements.lock SHA-256：`%s`"
        % (
            reproducibility["python_version"],
            reproducibility["requirements_lock_sha256"],
        ),
        "",
        source_note,
        "",
        "## 总体结果",
        "",
        "| 指标 | 单 Agent 基线 | 多 Agent 候选 | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for key, label in [
        ("precision", "Precision"),
        ("recall", "Recall"),
        ("f1", "F1"),
        ("severity_accuracy", "严重等级准确率"),
        ("high_risk_recall", "高风险召回率"),
        ("clean_accuracy", "干净 PR 准确率"),
        ("execution_success_rate", "执行成功率"),
    ]:
        lines.append(
            "| %s | %s | %s | %+.1f pp |"
            % (label, percent(b[key]), percent(c[key]), comparison["deltas"][key] * 100)
        )
    lines.extend(
        [
            "| 自动修复验证通过率 | — | %s | — |" % percent(c["safe_fix_rate"]),
            "| 端到端安全修复成功率 | — | %s | — |" % percent(c["e2e_security_fix_rate"]),
            "",
            "计数：基线 TP/FP/FN = %d/%d/%d；候选 TP/FP/FN = %d/%d/%d。"
            % (b["tp"], b["fp"], b["fn"], c["tp"], c["fp"], c["fn"]),
            "",
            "自动修复：%d 个白名单可修复风险全部进入修复流程，其中 %d 个通过风险复现、"
            "补丁生成、编译、风险消除和回归门禁；候选对另外 %d 个已命中但不满足白名单的"
            "风险安全弃权；全部 %d 个风险样本中 %d 个实现端到端成功。"
            % (
                c["repair_eligible"],
                c["repair_passed"],
                c["repair_abstained"],
                c["risk_cases"],
                c["e2e_successes"],
            ),
            "",
            "## 分区结果",
            "",
            "| 分区 | 样本 | 风险/干净 | F1 | 高风险召回率 | 干净准确率 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for split in ("validation", "holdout"):
        metrics = candidate["by_split"][split]
        lines.append(
            "| %s | %d | %d/%d | %s | %s | %s |"
            % (
                split.title(),
                metrics["cases"],
                metrics["risk_cases"],
                metrics["clean_cases"],
                percent(metrics["f1"]),
                percent(metrics["high_risk_recall"]),
                percent(metrics["clean_accuracy"]),
            )
        )
    lines.extend(
        [
            "",
            "## 质量切片与置信度",
            "",
            "| 语言 | 样本 | Precision | Recall | F1 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for language, metrics in candidate["by_language"].items():
        lines.append(
            "| %s | %d | %s | %s | %s |"
            % (
                language,
                metrics["cases"],
                percent(metrics["precision"]),
                percent(metrics["recall"]),
                percent(metrics["f1"]),
            )
        )
    lines.extend(
        [
            "",
            "| CWE | Expected | Predicted | Precision | Recall | F1 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for cwe, metrics in candidate["by_cwe"].items():
        lines.append(
            "| %s | %d | %d | %s | %s | %s |"
            % (
                cwe,
                metrics["expected"],
                metrics["predicted"],
                percent(metrics["precision"]),
                percent(metrics["recall"]),
                percent(metrics["f1"]),
            )
        )
    lines.extend(
        [
            "",
            "| Rule | Predicted | TP/FP | Precision | Mean confidence |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for rule, metrics in candidate["by_rule"].items():
        mean_confidence = metrics["mean_confidence"]
        lines.append(
            "| %s | %d | %d/%d | %s | %s |"
            % (
                rule,
                metrics["predicted"],
                metrics["tp"],
                metrics["fp"],
                percent(metrics["precision"]),
                percent(mean_confidence) if mean_confidence is not None else "—",
            )
        )
    calibration = candidate["confidence_calibration"]
    ece = calibration["expected_calibration_error"]
    brier = calibration["brier_score"]
    lines.extend(
        [
            "",
            "置信度口径仅衡量已报告 finding 是否正确：ECE=%s，Brier=%s，"
            "非法置信度=%d。"
            % (
                percent(ece) if ece is not None else "—",
                "%.4f" % brier if brier is not None else "—",
                calibration["invalid_confidences"],
            ),
        ]
    )
    lines.extend(
        [
            "",
            "## 指标口径",
            "",
            "- 一对一匹配：路径相同、CWE 相同，预测行位于标注区间或距离不超过 2 行。",
            "- 重复预测只能匹配一次，其余计为 FP。",
            "- 严重等级准确率仅在 TP 上计算，并要求等级完全一致。",
            "- 干净准确率按 PR 计算：干净 PR 完全没有报告才算正确。",
            "- 修复通过要求五个门禁全部成功：风险复现、补丁生成、编译、风险消除、回归检查。",
            "- 自动修复验证通过率以白名单可修复风险为分母；不满足确定性前置条件的风险会安全弃权。",
            "- 端到端安全修复成功率以全部风险样本为分母，同时反映识别、白名单覆盖和门禁结果。",
            "",
            "## 发布门禁",
            "",
            "数值门禁：**%s**"
            % ("通过" if comparison["release_gate"]["quantitative_passed"] else "未通过"),
            "",
            "生产激活：**%s**"
            % (
                "允许进入灰度"
                if comparison["release_gate"]["production_activation_allowed"]
                else "阻止；需使用带独立真值的真实公开 PR 数据集"
            ),
            "",
        ]
    )
    for name, gate in comparison["release_gate"]["gates"].items():
        lines.append("- `%s`：%s" % (name, "PASS" if gate["passed"] else "FAIL"))
    lines.extend(
        [
            "",
        ]
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", default=os.path.join(ROOT, "evaluation_data", "pr_diff_100.jsonl")
    )
    parser.add_argument("--output-dir", default=os.path.join(ROOT, "output", "evaluation"))
    parser.add_argument(
        "--annotation-evidence",
        help="Sidecar JSON emitted by evoagent-eval-labels for production provenance gating",
    )
    parser.add_argument(
        "--reuse-dataset",
        action="store_true",
        help="Load the existing JSONL instead of regenerating the controlled corpus.",
    )
    args = parser.parse_args()

    if args.reuse_dataset:
        cases = load_jsonl(args.dataset)
    else:
        cases = generate_controlled_pr_cases()
        write_jsonl(args.dataset, cases)

    annotation_evidence = None
    if args.annotation_evidence:
        with open(args.annotation_evidence, "rb") as handle:
            annotation_evidence = strict_json_loads(handle.read())
        if not isinstance(annotation_evidence, dict):
            raise ValueError("annotation evidence must be a JSON object")

    baseline = EndToEndEvaluationHarness().run(
        baseline_reviewer(),
        cases,
        "single-agent-baseline",
        annotation_evidence,
    )
    candidate = EndToEndEvaluationHarness(repairer=FixtureRepairer()).run(
        candidate_reviewer(),
        cases,
        "multi-agent-candidate",
        annotation_evidence,
    )
    comparison = comparison_summary(baseline, candidate)
    reproducibility = reproducibility_metadata()
    report = {
        "schema_version": 3,
        "reproducibility": reproducibility,
        "baseline": baseline,
        "candidate": candidate,
        "comparison": comparison,
    }
    os.makedirs(args.output_dir, exist_ok=True)
    json_path = os.path.join(args.output_dir, "evaluation-report.json")
    markdown_path = os.path.join(args.output_dir, "evaluation-report.md")
    with open(json_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with open(markdown_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(markdown_report(baseline, candidate, comparison, reproducibility))

    print("dataset:", args.dataset)
    print("report:", json_path)
    print(
        "baseline F1=%s candidate F1=%s high-risk recall=%s clean accuracy=%s"
        % (
            percent(baseline["metrics"]["f1"]),
            percent(candidate["metrics"]["f1"]),
            percent(candidate["metrics"]["high_risk_recall"]),
            percent(candidate["metrics"]["clean_accuracy"]),
        )
    )
    print(
        "safe fix=%s e2e fix=%s"
        % (
            percent(candidate["metrics"]["safe_fix_rate"]),
            percent(candidate["metrics"]["e2e_security_fix_rate"]),
        )
    )


if __name__ == "__main__":
    main()
