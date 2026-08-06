"""Generate/replay the 100-case benchmark and write JSON plus Markdown reports."""
import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.evaluation_benchmark import (  # noqa: E402
    baseline_reviewer,
    candidate_reviewer,
    generate_controlled_pr_cases,
)
from evoagent.evaluation_harness import (  # noqa: E402
    EndToEndEvaluationHarness,
    FixtureRepairer,
    comparison_summary,
    load_jsonl,
)


def write_jsonl(path, cases):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")


def percent(value):
    return "%.1f%%" % (100 * value)


def markdown_report(baseline, candidate, comparison):
    b = baseline["metrics"]
    c = candidate["metrics"]
    dataset = candidate["dataset"]
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
        "",
        "> 注意：本次离线数据是受控合成基准，用于验证评测代码和计算口径，"
        "不能表述为 100 个真实公开 PR 的生产效果。",
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
    lines.extend([
        "| 自动修复验证通过率 | — | %s | — |" % percent(c["safe_fix_rate"]),
        "| 端到端安全修复成功率 | — | %s | — |"
        % percent(c["e2e_security_fix_rate"]),
        "",
        "计数：基线 TP/FP/FN = %d/%d/%d；候选 TP/FP/FN = %d/%d/%d。"
        % (b["tp"], b["fp"], b["fn"], c["tp"], c["fp"], c["fn"]),
        "",
        "自动修复：候选命中的 %d 个风险中，%d 个通过风险复现、补丁生成、"
        "编译、风险消除和回归门禁；全部 %d 个风险样本中 %d 个实现端到端成功。"
        % (
            c["repair_attempted"], c["repair_passed"],
            c["risk_cases"], c["e2e_successes"],
        ),
        "",
        "## 分区结果",
        "",
        "| 分区 | 样本 | 风险/干净 | F1 | 高风险召回率 | 干净准确率 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for split in ("validation", "holdout"):
        metrics = candidate["by_split"][split]
        lines.append(
            "| %s | %d | %d/%d | %s | %s | %s |"
            % (
                split.title(), metrics["cases"], metrics["risk_cases"],
                metrics["clean_cases"], percent(metrics["f1"]),
                percent(metrics["high_risk_recall"]), percent(metrics["clean_accuracy"]),
            )
        )
    lines.extend([
        "",
        "## 指标口径",
        "",
        "- 一对一匹配：路径相同、CWE 相同，预测行位于标注区间或距离不超过 2 行。",
        "- 重复预测只能匹配一次，其余计为 FP。",
        "- 严重等级准确率仅在 TP 上计算，并要求等级完全一致。",
        "- 干净准确率按 PR 计算：干净 PR 完全没有报告才算正确。",
        "- 修复通过要求五个门禁全部成功：风险复现、补丁生成、编译、风险消除、回归检查。",
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
    ])
    for name, gate in comparison["release_gate"]["gates"].items():
        lines.append(
            "- `%s`：%s" % (name, "PASS" if gate["passed"] else "FAIL")
        )
    lines.extend([
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", default=os.path.join(ROOT, "evaluation_data", "pr_diff_100.jsonl")
    )
    parser.add_argument(
        "--output-dir", default=os.path.join(ROOT, "output", "evaluation")
    )
    parser.add_argument(
        "--reuse-dataset", action="store_true",
        help="Load the existing JSONL instead of regenerating the controlled corpus.",
    )
    args = parser.parse_args()

    if args.reuse_dataset:
        cases = load_jsonl(args.dataset)
    else:
        cases = generate_controlled_pr_cases()
        write_jsonl(args.dataset, cases)

    baseline = EndToEndEvaluationHarness().run(
        baseline_reviewer(), cases, "single-agent-baseline"
    )
    candidate = EndToEndEvaluationHarness(repairer=FixtureRepairer()).run(
        candidate_reviewer(), cases, "multi-agent-candidate"
    )
    comparison = comparison_summary(baseline, candidate)
    report = {
        "schema_version": 1,
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
        handle.write(markdown_report(baseline, candidate, comparison))

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
