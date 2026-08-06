import re

from evoagent.models import Finding, Severity
from evoagent.reviewer import Reviewer

SKILL_NAME = "code-quality"
SKILL_VERSION = "1.0.0"
SKILL_DESCRIPTION = "Detects unfinished TODO/FIXME markers in added production code"


class CodeQualitySkill(Reviewer):
    name = "code-quality-agent"

    def review(self, diff, parsed):
        findings = []
        for line in parsed.added_lines:
            if re.search(r"\b(TODO|FIXME)\b", line.content) and not line.path.startswith("tests/"):
                findings.append(Finding(
                    rule_id="QUALITY-UNFINISHED", severity=Severity.LOW,
                    title="新增代码包含未完成标记",
                    explanation="提交中留下了 TODO/FIXME，可能代表尚未实现的生产路径。",
                    path=line.path, line=line.line, evidence=line.content.strip(),
                    fix="在合并前实现该逻辑，或关联有负责人和期限的跟踪任务。",
                    test="增加覆盖该未完成路径的测试，并验证期望行为。", confidence=0.8,
                ))
        return findings


def create_skill():
    return CodeQualitySkill()

