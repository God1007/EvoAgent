import json
import re
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .diff_parser import ParsedDiff
from .models import Finding, Severity


class Reviewer(ABC):
    name = "reviewer"

    @abstractmethod
    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
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
            re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\b\s*=\s*['\"][^'\"]{4,}['\"]"),
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

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        findings: List[Finding] = []
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


class OpenAICompatibleReviewer(Reviewer):
    name = "openai-compatible"

    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: int = 60,
        system_prompt: str = "", provider: str = "openai-compatible",
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.system_prompt = system_prompt
        self.provider = provider
        self.name = "%s:%s" % (provider, model)
        self.extra_headers = extra_headers or {}

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        schema = (
            'Return JSON only: {"findings":[{"rule_id":"...","severity":"critical|high|medium|low",'
            '"title":"...","explanation":"...","path":"...","line":1,"evidence":"...",'
            '"fix":"...","test":"...","confidence":0.0}]}. Report only actionable defects introduced '
            "by added lines. Do not report style preferences. Line numbers must be new-file line numbers."
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": (self.system_prompt or "You are a senior secure code reviewer.") + " " + schema},
                {"role": "user", "content": "Review this unified diff:\n\n" + diff},
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.extra_headers)
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError("%s API returned HTTP %d: %s" % (self.provider, exc.code, detail)) from exc
        except (urllib.error.URLError, socket.timeout, ValueError, KeyError) as exc:
            raise RuntimeError("%s review request failed: %s" % (self.provider, exc)) from exc
        try:
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("%s returned an invalid JSON review response" % self.provider) from exc
        valid_locations = {(item.path, item.line) for item in parsed.added_lines}
        findings: List[Finding] = []
        for raw in result.get("findings", []):
            path, line = str(raw.get("path", "")), int(raw.get("line", 0))
            if (path, line) not in valid_locations:
                continue
            try:
                severity = Severity(str(raw.get("severity", "medium")).lower())
            except ValueError:
                severity = Severity.MEDIUM
            findings.append(
                Finding(
                    rule_id=str(raw.get("rule_id", "LLM-REVIEW"))[:80],
                    severity=severity,
                    title=str(raw.get("title", "Review finding"))[:200],
                    explanation=str(raw.get("explanation", ""))[:2000],
                    path=path,
                    line=line,
                    evidence=str(raw.get("evidence", ""))[:240],
                    fix=str(raw.get("fix", ""))[:2000],
                    test=str(raw.get("test", ""))[:2000],
                    confidence=max(0.0, min(1.0, float(raw.get("confidence", 0.7)))),
                )
            )
        return findings


class CompositeReviewer(Reviewer):
    name = "composite"

    def __init__(self, reviewers: List[Reviewer]):
        self.reviewers = reviewers
        self.name = "+".join(item.name for item in reviewers)

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        merged: Dict[Any, Finding] = {}
        errors = []
        for reviewer in self.reviewers:
            try:
                for finding in reviewer.review(diff, parsed):
                    key = (finding.path, finding.line, finding.rule_id)
                    merged[key] = finding
            except Exception as exc:
                errors.append(exc)
        if not merged and errors and len(errors) == len(self.reviewers):
            raise errors[0]
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(merged.values(), key=lambda item: (order[item.severity], item.path, item.line))
