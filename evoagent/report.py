import re
from typing import Any

# Any ASCII punctuation character may be backslash-escaped in CommonMark and
# renders as the literal character, so escaping the whole class neutralises
# every Markdown construct (headings, lists, tables, blockquotes, links,
# images, raw HTML, code spans) while preserving the visible text.
_ASCII_PUNCT = re.compile(r"([!-/:-@\[-`{-~])")


def _escape(value: Any) -> str:
    """Render untrusted free text so it cannot inject any Markdown structure."""
    return _ASCII_PUNCT.sub(r"\\\1", str(value))


def _text(value: Any) -> str:
    """Collapse untrusted text to a single escaped line for use in a heading."""
    return _escape(str(value).replace("\r", " ").replace("\n", " ").strip())


def _code(value: Any) -> str:
    """Render untrusted text as a safe inline code span.

    Backticks and newlines are neutralised so a crafted value (path, rule id,
    severity) cannot break out of the span and inject Markdown.
    """
    text = str(value).replace("\r", " ").replace("\n", " ").replace("`", "'")
    return "`%s`" % text


def _fence(value: Any) -> str:
    """Wrap untrusted text in a fenced block that it cannot escape.

    Per CommonMark a fenced code block is closed only by a run of backticks at
    least as long as its opening fence, so the opening fence is made strictly
    longer than the longest backtick run inside the content.
    """
    text = str(value)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return "%s\n%s\n%s" % (fence, text, fence)


def to_markdown(report: dict[str, Any]) -> str:
    title = "# EvoAgent PR Review"
    if report.get("pull_request") is not None:
        title += " — #%s" % _text(report["pull_request"])
    lines = [
        title,
        "",
        "**Repository:** %s  " % _code(report.get("repository", "")),
        "**Risk:** %s  " % _code(report.get("risk", "unknown")),
        "**Reviewer:** %s" % _code(report.get("reviewer", "unknown")),
        "",
        _escape(report.get("summary", "")),
        "",
    ]
    findings = report.get("findings", [])
    if not findings:
        lines.append("✅ No actionable issue detected in the added lines.")
        return "\n".join(lines) + "\n"
    lines.extend(["## Findings", ""])
    icons = {"critical": "🚨", "high": "🔴", "medium": "🟠", "low": "🟡"}
    for index, item in enumerate(findings, 1):
        severity = str(item.get("severity", "medium"))
        heading = "### %d. %s %s" % (
            index,
            icons.get(severity, "•"),
            _text(item.get("title", "Finding")),
        )
        location = "%s · **%s** · %s" % (
            _code("%s:%s" % (item.get("path", ""), item.get("line", 0))),
            _text(severity).upper(),
            _code(item.get("rule_id", "")),
        )
        lines.extend(
            [
                heading,
                "",
                location,
                "",
                _escape(item.get("explanation", "")),
                "",
                "**Evidence**",
                "",
                _fence(item.get("evidence", "")),
                "",
                "**Suggested fix:** %s" % _escape(item.get("fix", "")),
                "",
                "**Suggested test:** %s" % _escape(item.get("test", "")),
                "",
            ]
        )
    return "\n".join(lines) + "\n"
