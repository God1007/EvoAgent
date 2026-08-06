"""Deterministic benchmark corpus and reviewers for local Evaluation Harness demos."""
import difflib
import re
from typing import Dict, List, Tuple

from .agents import MultiAgentCoordinator
from .diff_parser import ParsedDiff, parse_unified_diff
from .evaluation_harness import RULE_TO_CWE
from .models import Finding, Severity
from .reviewer import LocalRuleReviewer, Reviewer


class ContextRuleReviewer(Reviewer):
    """Additional specialist used to demonstrate candidate-vs-baseline replay."""

    name = "context-security-reliability-agent"
    RULES = [
        ("SEC-PATH-TRAVERSAL", Severity.HIGH, re.compile(r"open\(base\s*/\s*user_path\)")),
        ("SEC-YAML-LOAD", Severity.HIGH, re.compile(r"\byaml\.load\s*\(")),
        ("SEC-WEAK-HASH", Severity.MEDIUM, re.compile(r"\bhashlib\.md5\s*\(")),
        ("SEC-INSECURE-TEMPFILE", Severity.MEDIUM, re.compile(r"\btempfile\.mktemp\s*\(")),
        ("SEC-WEAK-RANDOM", Severity.MEDIUM, re.compile(r"\brandom\.random\s*\(")),
        ("REL-UNBOUNDED-RETRY", Severity.MEDIUM, re.compile(r"^\s*while\s+True\s*:")),
        ("SEC-ASSERT-AUTH", Severity.MEDIUM, re.compile(r"^\s*assert\s+user\.is_admin")),
        (
            "SEC-INSECURE-COOKIE", Severity.MEDIUM,
            re.compile(r"set_cookie\(.+secure\s*=\s*False"),
        ),
    ]

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        findings = []
        for line in parsed.added_lines:
            for rule_id, severity, pattern in self.RULES:
                if not pattern.search(line.content):
                    continue
                findings.append(Finding(
                    rule_id=rule_id,
                    severity=severity,
                    title="Context-sensitive benchmark finding",
                    explanation=(
                        "The changed line matches a context-sensitive security or reliability "
                        "risk that requires evidence review."
                    ),
                    path=line.path,
                    line=line.line,
                    evidence=line.content.strip()[:240],
                    fix="Replace the unsafe operation with a constrained, validated alternative.",
                    test="Add a focused reproduction and run compilation plus regression tests.",
                    confidence=0.86,
                ))
        return findings


def baseline_reviewer() -> Reviewer:
    return LocalRuleReviewer()


def candidate_reviewer() -> Reviewer:
    return MultiAgentCoordinator([LocalRuleReviewer(), ContextRuleReviewer()])


def _risk_scenarios() -> List[dict]:
    scenarios = []

    def add(
        rule_id: str, severity: str, line: str, risk_pattern: str,
        required: str, count: int = 1, repairable: bool = True,
    ) -> None:
        for number in range(count):
            scenarios.append({
                "name": "%s-%02d" % (rule_id.lower(), number + 1),
                "rule_id": rule_id,
                "cwe": RULE_TO_CWE[rule_id],
                "severity": severity,
                "line": line.format(n=number + 1),
                "risk_pattern": risk_pattern,
                "required_after_patterns": [required] if required else [],
                "auto_fixable": repairable,
            })

    # 19 high/critical cases: baseline finds 16, candidate finds another 2.
    add("SEC-EVAL", "critical", "result = eval(value)", r"\beval\s*\(", r"json\.loads", 4)
    add(
        "SEC-SUBPROCESS-SHELL", "high",
        "result = subprocess.run(value, shell=True)",
        r"shell\s*=\s*True", r"shell\s*=\s*False", 4,
    )
    add(
        "SEC-HARDCODED-SECRET", "high",
        'api_key = "production-secret-{n}"',
        r"api_key\s*=\s*['\"]production-secret", r"os\.environ", 4,
    )
    add(
        "SEC-SQL-CONCAT", "high",
        'cursor.execute("SELECT * FROM users WHERE id=" + value)',
        r"cursor\.execute\(.+\+", r"\(value,\)", 4,
    )
    add(
        "SEC-PATH-TRAVERSAL", "high",
        "return open(base / user_path).read()",
        r"open\(base\s*/\s*user_path\)", r"read_under_base", 1,
    )
    add(
        "SEC-YAML-LOAD", "high", "return yaml.load(value)",
        r"yaml\.load", r"yaml\.safe_load", 1, False,
    )
    add(
        "SEC-PICKLE-LOAD", "high", "return pickle.loads(value)",
        r"pickle\.loads", r"json\.loads", 1, False,
    )

    # 21 medium/low cases: baseline finds 9, candidate another 6, seven remain missed.
    add(
        "REL-EMPTY-EXCEPT", "medium", "except Exception:",
        r"except Exception:", r"except ValueError:", 5,
    )
    add(
        "REL-DEBUG-PRINT", "low", "print(value)",
        r"print\s*\(", r"return value", 4,
    )
    add(
        "SEC-WEAK-HASH", "medium", "digest = hashlib.md5(value).hexdigest()",
        r"hashlib\.md5", r"hashlib\.sha256", 1, False,
    )
    add(
        "SEC-INSECURE-TEMPFILE", "medium", "path = tempfile.mktemp()",
        r"tempfile\.mktemp", r"NamedTemporaryFile", 1, False,
    )
    add(
        "SEC-WEAK-RANDOM", "medium", "token = str(random.random())",
        r"random\.random", r"secrets\.token", 1, False,
    )
    add(
        "REL-UNBOUNDED-RETRY", "medium", "while True:",
        r"while True:", r"max_attempts", 1, False,
    )
    add(
        "SEC-ASSERT-AUTH", "medium", "assert user.is_admin",
        r"assert user\.is_admin", r"PermissionError", 1, False,
    )
    add(
        "SEC-INSECURE-COOKIE", "medium",
        'response.set_cookie("sid", value, secure=False)',
        r"secure\s*=\s*False", r"secure\s*=\s*True", 1, False,
    )
    add(
        "REL-FLOAT-MONEY", "medium", "total = float(value) * 100",
        r"float\(value\)", r"Decimal", 1, False,
    )
    add(
        "REL-NAIVE-DATETIME", "medium", "expires_at = datetime.now()",
        r"datetime\.now\(\)", r"timezone\.utc", 1, False,
    )
    add(
        "REL-BLOCKING-ASYNC", "medium", "time.sleep(5)",
        r"time\.sleep", r"await asyncio\.sleep", 1, False,
    )
    add(
        "REL-NONATOMIC-WRITE", "medium", 'open("state.json", "w").write(value)',
        r"open\(.+state\.json", r"os\.replace", 1, False,
    )
    add(
        "SEC-OPEN-REDIRECT", "medium", "return redirect(value)",
        r"redirect\(value\)", r"allowed_hosts", 1, False,
    )
    add(
        "SEC-LOG-FORGING", "low", "logger.info(value)",
        r"logger\.info\(value\)", r"sanitize", 1, False,
    )
    if len(scenarios) != 40:
        raise AssertionError("benchmark must contain exactly 40 risk scenarios")
    return scenarios


def _risk_source(scenario: dict) -> Tuple[str, str, str]:
    line = scenario["line"]
    if scenario["rule_id"] == "REL-EMPTY-EXCEPT":
        before = (
            "def process(value):\n"
            "    try:\n"
            "        return int(value)\n"
            "    except ValueError:\n"
            "        return None\n"
        )
        after = before.replace("except ValueError:", "except Exception:")
    elif scenario["rule_id"] == "REL-UNBOUNDED-RETRY":
        before = (
            "def process(value):\n"
            "    for attempt in range(3):\n"
            "        if send(value):\n"
            "            return True\n"
            "    return False\n"
        )
        after = (
            "def process(value):\n"
            "    while True:\n"
            "        if send(value):\n"
            "            return True\n"
        )
    else:
        before = "def process(value):\n    normalized = str(value)\n    return normalized\n"
        after = (
            "def process(value):\n"
            "    normalized = str(value)\n"
            "    %s\n"
            "    return value\n" % line
        )
    return before, after, line.strip()


def _clean_source(index: int) -> Tuple[str, str]:
    before = "def process(value):\n    return value\n"
    if index < 5:
        extra = '    token = "test-placeholder"\n'
        if index < 2:
            extra += '    checksum = hashlib.md5(b"fixture-id").hexdigest()\n'
        after = "def process(value):\n%s    return value\n" % extra
    else:
        variants = [
            "    limit = min(max(int(value), 1), 100)\n",
            '    cursor.execute("SELECT * FROM users WHERE id = ?", (value,))\n',
            '    api_key = os.environ["API_KEY"]\n',
            "    digest = hashlib.sha256(str(value).encode()).hexdigest()\n",
            "    response.set_cookie(\"sid\", value, secure=True)\n",
        ]
        after = "def process(value):\n%s    return value\n" % variants[index % len(variants)]
    return before, after


def _unified_diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(True),
        after.splitlines(True),
        fromfile="a/" + path,
        tofile="b/" + path,
        n=3,
    ))


def generate_controlled_pr_cases() -> List[dict]:
    """Generate 100 reproducible PR-like diffs.

    These are deliberately labelled synthetic-controlled. They are useful for testing
    the harness, not for making claims about production performance on public PRs.
    """
    repositories = ["acme/service-%02d" % number for number in range(1, 11)]
    by_repo: Dict[str, List[dict]] = {repository: [] for repository in repositories}
    scenarios = _risk_scenarios()
    for index, scenario in enumerate(scenarios):
        repository = repositories[index % len(repositories)]
        path = "src/change_%02d.py" % (index + 1)
        before, after, needle = _risk_source(scenario)
        diff = _unified_diff(path, before, after)
        added = parse_unified_diff(diff).added_lines
        target = next(item for item in added if item.content.strip() == needle)
        by_repo[repository].append({
            "kind": "risk",
            "path": path,
            "diff": diff,
            "after": after,
            "scenario": scenario,
            "target_line": target.line,
        })
    for index in range(60):
        repository = repositories[index % len(repositories)]
        path = "src/clean_%02d.py" % (index + 1)
        before, after = _clean_source(index)
        by_repo[repository].append({
            "kind": "clean", "path": path,
            "diff": _unified_diff(path, before, after), "after": after,
        })

    cases = []
    sequence = 1
    for repository_index, repository in enumerate(repositories):
        split = "validation" if repository_index < 8 else "holdout"
        for local_index, item in enumerate(by_repo[repository], 1):
            expected = []
            repair_validation = {}
            if item["kind"] == "risk":
                scenario = item["scenario"]
                expected = [{
                    "path": item["path"],
                    "start_line": item["target_line"],
                    "end_line": item["target_line"],
                    "cwe": scenario["cwe"],
                    "rule_id": scenario["rule_id"],
                    "severity": scenario["severity"],
                }]
                repair_validation = {
                    "auto_fixable": scenario["auto_fixable"],
                    "risk_pattern": scenario["risk_pattern"],
                    "required_after_patterns": scenario["required_after_patterns"],
                }
            cases.append({
                "schema_version": 1,
                "id": "pr-%04d" % sequence,
                "repository": repository,
                "pull_request": 1000 + local_index,
                "split": split,
                "source": {
                    "kind": "synthetic-controlled",
                    "generator": "evoagent-e2e-v1",
                    "public_url": None,
                },
                "diff": item["diff"],
                "after_files": {item["path"]: item["after"]},
                "expected_findings": expected,
                "repair_validation": repair_validation,
            })
            sequence += 1
    if len(cases) != 100:
        raise AssertionError("benchmark must contain exactly 100 cases")
    return cases
