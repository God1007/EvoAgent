import math
import re
from abc import ABC, abstractmethod
from collections.abc import Callable

from .diff_parser import ParsedDiff
from .json_boundary import strict_json_loads
from .model_gateway import ModelGovernanceContext, ModelMessage, ModelRequest
from .models import Finding, Severity
from .ports import ModelGatewayPort

# ponytail: fixed per-reviewer cap; raise it only when 100 findings prove insufficient.
MAX_REVIEWER_FINDINGS = 100
MAX_REVIEWER_NAME_CHARS = 100


def valid_reviewer_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_REVIEWER_NAME_CHARS
        and all(character.isprintable() and not character.isspace() for character in value)
    )


class Reviewer(ABC):
    name = "reviewer"

    @abstractmethod
    def review(self, diff: str, parsed: ParsedDiff) -> list[Finding]:
        raise NotImplementedError


class LocalRuleReviewer(Reviewer):
    name = "local-rules"

    RULES = [
        (
            "SEC-EVAL",
            Severity.CRITICAL,
            re.compile(r"\b(eval|exec)\s*\("),
            "动态代码执行可能导致注入",
            "新增代码调用了动态执行函数；当参数可被外部影响时，攻击者可能执行任意代码。",
            "移除动态执行；使用显式解析器、命令映射表或严格白名单处理输入。",
            "加入恶意表达式与边界输入测试，断言输入不会被当作代码执行。",
        ),
        (
            "SEC-SUBPROCESS-SHELL",
            Severity.HIGH,
            re.compile(r"\bshell\s*=\s*True\b"),
            "Shell 调用存在命令注入风险",
            "shell=True 会扩大参数拼接造成命令注入的风险。",
            "使用参数数组并保持 shell=False；对允许值进行白名单验证。",
            "加入包含空格、分号与命令替换字符的输入测试。",
        ),
        (
            "SEC-HARDCODED-SECRET",
            Severity.HIGH,
            re.compile(
                r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b\s*=\s*['\"][^'\"]{4,}['\"]"
            ),
            "疑似硬编码凭据",
            "凭据进入代码仓库后可能通过历史记录、构建日志或制品泄露。",
            "从密钥管理服务或环境变量读取，并立即轮换已经提交的凭据。",
            "测试缺少配置时安全失败，且日志不会输出凭据。",
        ),
        (
            "SEC-SQL-CONCAT",
            Severity.HIGH,
            re.compile(r"(?i)(execute|query)\s*\(\s*(f['\"]|['\"].*(\+|%))"),
            "SQL 语句疑似动态拼接",
            "将外部数据拼接到 SQL 中可能产生 SQL 注入。",
            "改用驱动提供的参数化查询与占位符。",
            "加入引号、注释符和布尔表达式等注入载荷测试。",
        ),
        (
            "SEC-YAML-LOAD",
            Severity.HIGH,
            re.compile(r"\byaml\.load\s*\("),
            "不安全的 YAML 反序列化",
            "yaml.load 在 Loader 不受约束时可能构造任意 Python 对象并触发代码执行。",
            "对于普通数据使用 yaml.safe_load；只有明确需要且输入可信时才选择受限 Loader。",
            "加入恶意对象标签和普通映射测试，断言不会实例化任意对象。",
        ),
        (
            "SEC-INSECURE-COOKIE",
            Severity.HIGH,
            re.compile(r"\bset_cookie\s*\(.*\bsecure\s*=\s*False\b"),
            "Cookie 显式关闭 Secure 属性",
            "认证或会话 Cookie 可通过明文 HTTP 发送，增加被窃取和会话劫持的风险。",
            "对承载敏感状态的 Cookie 启用 secure=True，并结合 HttpOnly 与 SameSite 策略。",
            "在 HTTPS 响应测试中断言 Set-Cookie 同时包含 Secure 和预期的 SameSite 属性。",
        ),
        (
            "REL-EMPTY-EXCEPT",
            Severity.MEDIUM,
            re.compile(r"^\s*except\s*(Exception\s*)?:\s*(pass)?\s*$"),
            "异常被宽泛捕获",
            "宽泛捕获会隐藏真实故障，使调用方误以为操作成功。",
            "仅捕获可处理的异常，记录必要上下文，并让不可恢复错误向上传播。",
            "加入依赖失败测试，断言错误可观察且不会返回伪成功。",
        ),
        (
            "REL-DEBUG-PRINT",
            Severity.LOW,
            re.compile(r"\b(print\s*\(|console\.log\s*\()"),
            "新增调试输出",
            "直接输出可能污染服务日志或意外暴露运行数据。",
            "删除调试输出，或改用带级别和脱敏策略的结构化日志。",
            "验证正常请求不会产生包含敏感值的非预期输出。",
        ),
    ]

    def review(self, diff: str, parsed: ParsedDiff) -> list[Finding]:
        findings: list[Finding] = []
        seen = set()
        for line in parsed.added_lines:
            if line.path.endswith((".lock", ".min.js", ".map")):
                continue
            for rule_id, severity, pattern, title, explanation, fix, test in self.RULES:
                if pattern.search(line.content) and (rule_id, line.path, line.line) not in seen:
                    seen.add((rule_id, line.path, line.line))
                    findings.append(
                        Finding(
                            rule_id=rule_id,
                            severity=severity,
                            title=title,
                            explanation=explanation,
                            path=line.path,
                            line=line.line,
                            evidence=line.content.strip()[:240],
                            fix=fix,
                            test=test,
                            confidence=0.9,
                        )
                    )
        return findings


def _review_messages(diff: str, system_prompt: str = "") -> tuple[ModelMessage, ...]:
    schema = (
        'Return JSON only: {"findings":[{"rule_id":"...","severity":"critical|high|medium|low",'
        '"title":"...","explanation":"...","path":"...","line":1,"evidence":"...",'
        '"fix":"...","test":"...","confidence":0.0}]}. Report only actionable defects introduced '
        "by added lines. Do not report style preferences. Line numbers must be new-file line numbers."
    )
    return (
        ModelMessage(
            "system",
            (system_prompt or "You are a senior secure code reviewer.") + " " + schema,
        ),
        ModelMessage("user", "Review this unified diff:\n\n" + diff),
    )


def _parse_model_findings(content: str, parsed: ParsedDiff) -> list[Finding]:
    try:
        result = strict_json_loads(content)
    except (TypeError, UnicodeError, ValueError, RecursionError) as exc:
        raise RuntimeError("model returned an invalid JSON review response") from exc
    if not isinstance(result, dict) or not isinstance(result.get("findings"), list):
        raise RuntimeError("model returned an invalid JSON review response")
    if len(result["findings"]) > MAX_REVIEWER_FINDINGS:
        raise RuntimeError("model returned too many findings")
    valid_locations = {(item.path, item.line) for item in parsed.added_lines}
    findings: list[Finding] = []
    for raw in result["findings"]:
        if not isinstance(raw, dict):
            continue
        path, line = raw.get("path"), raw.get("line")
        if not isinstance(path, str) or not isinstance(line, int) or isinstance(line, bool):
            continue
        if (path, line) not in valid_locations:
            continue
        try:
            severity = Severity(str(raw.get("severity", "medium")).lower())
        except ValueError:
            severity = Severity.MEDIUM
        raw_confidence = raw.get("confidence", 0.7)
        if isinstance(raw_confidence, int) and not isinstance(raw_confidence, bool):
            confidence = float(max(0, min(1, raw_confidence)))
        elif isinstance(raw_confidence, float) and math.isfinite(raw_confidence):
            confidence = max(0.0, min(1.0, raw_confidence))
        else:
            confidence = 0.0
        try:
            finding = Finding.from_dict(
                {
                    "rule_id": raw.get("rule_id", "LLM-REVIEW"),
                    "severity": severity,
                    "title": raw.get("title", "Review finding"),
                    "explanation": raw.get("explanation", ""),
                    "path": path,
                    "line": line,
                    "evidence": raw.get("evidence", ""),
                    "fix": raw.get("fix", ""),
                    "test": raw.get("test", ""),
                    "confidence": confidence,
                }
            )
        except ValueError:
            continue
        findings.append(finding)
    return findings


class GatewayReviewer(Reviewer):
    """Domain reviewer backed by the governed ModelGatewayPort."""

    def __init__(
        self,
        gateway: ModelGatewayPort,
        task_context: Callable[[str], ModelGovernanceContext],
        system_prompt: str = "",
    ):
        self.gateway = gateway
        self.task_context = task_context
        self.system_prompt = system_prompt
        route = gateway.route_info()
        self.name = "%s:%s" % (route.get("provider", "model"), route.get("model", "unknown"))

    def review(self, diff: str, parsed: ParsedDiff) -> list[Finding]:
        return self._review(
            "",
            ModelGovernanceContext("system", "evaluation"),
            "evaluation",
            diff,
            parsed,
        )

    def review_with_context(
        self,
        task_id: str,
        diff: str,
        parsed: ParsedDiff,
        admission_generation: int | None = None,
    ) -> list[Finding]:
        return self._review(task_id, self.task_context(task_id), "review", diff, parsed)

    def _review(
        self,
        task_id: str,
        context: ModelGovernanceContext,
        purpose: str,
        diff: str,
        parsed: ParsedDiff,
    ) -> list[Finding]:
        response = self.gateway.complete(
            ModelRequest(
                tenant_id=context.tenant_id,
                repository=context.repository,
                task_id=task_id,
                purpose=purpose,
                messages=_review_messages(diff, self.system_prompt),
                allowed_providers=context.allowed_providers,
                allowed_models=context.allowed_models,
                required_region=context.required_region,
            )
        )
        return _parse_model_findings(response.content, parsed)
