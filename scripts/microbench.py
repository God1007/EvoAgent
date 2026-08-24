#!/usr/bin/env python3
"""Micro-benchmarks for EvoAgent hot code paths.

Complements the HTTP load test (`scripts/loadgen.py`): this measures pure-CPU
functions that sit on the request path so an accidental O(n^2) or a heavy import
in a tight loop is caught before it shows up as tail latency in production.

Uses stdlib `timeit`, reports ns/op, and supports per-benchmark regression
thresholds (fail if slower than budget) plus JSON output for CI trend tracking.

    python scripts/microbench.py                 # human table
    python scripts/microbench.py --json out.json # machine-readable
    python scripts/microbench.py --check         # enforce built-in budgets
"""

import argparse
import json
import sys
import timeit

from evoagent.codegraph import build_graph
from evoagent.diff_parser import parse_unified_diff
from evoagent.models import Finding, Severity
from evoagent.session import classify_findings, snapshot_findings

# ns/op budgets: generous ceilings meant to catch order-of-magnitude regressions,
# not micro-noise. These are the CI gate, so they must clear the SLOWEST runner
# (shared 2-core GitHub runners are ~3x slower than a fast dev box) with headroom
# for noise. Observed ns/op (fast dev box / GitHub CI): parse 111k/349k,
# fingerprint 2.2k/6.4k, classify 157k/456k, codegraph 56k/161k. Budgets sit at
# ~3x the CI figure - loose enough not to flake, tight enough to catch a >3x
# regression. Recalibrate only from repeated runs on the slowest supported CI host.
BUDGETS_NS = {
    "parse_unified_diff": 1_000_000,
    "parse_many_files": 50_000_000,
    "finding_fingerprint": 50_000,
    "scoped_fingerprint": 50_000,
    "classify_findings": 1_500_000,
    "codegraph_impact_of": 2_000_000,
}


def _make_diff(files: int = 20, lines: int = 20) -> str:
    chunks = []
    for f in range(files):
        chunks.append("--- a/mod%d.py\n+++ b/mod%d.py\n@@ -1,%d +1,%d @@" % (f, f, lines, lines))
        for line in range(lines):
            chunks.append("+    value_%d = compute(%d)" % (line, line))
    return "\n".join(chunks) + "\n"


def _make_findings(count: int = 50) -> list[Finding]:
    return [
        Finding(
            rule_id="RULE-%d" % (i % 10),
            severity=Severity.MEDIUM,
            title="Issue number %d in the module" % i,
            explanation="explanation text " * 5,
            path="pkg/mod%d.py" % (i % 8),
            line=i,
            evidence="eval(user_input_%d)  # dangerous call" % i,
            fix="use ast.literal_eval",
            test="",
            confidence=0.7,
        )
        for i in range(count)
    ]


def _make_sources(modules: int = 30) -> dict[str, str]:
    sources = {}
    for m in range(modules):
        callee = "mod%d" % ((m + 1) % modules)
        sources["pkg/mod%d.py" % m] = (
            "from pkg.%s import helper\n\n"
            "def helper():\n    return %d\n\n"
            "def run():\n    return helper() + %s.helper()\n" % (callee, m, callee)
        )
    return sources


def _benchmarks():
    diff = _make_diff()
    many_files = "".join("+++ b/f%d.py\n" % index for index in range(10_000))
    findings = _make_findings()
    one_finding = findings[0]
    previous = snapshot_findings("org/repo", findings)
    current = _make_findings(60)
    sources = _make_sources()
    graph = build_graph(sources)

    return {
        "parse_unified_diff": (lambda: parse_unified_diff(diff), 2000),
        "parse_many_files": (lambda: parse_unified_diff(many_files), 20),
        "finding_fingerprint": (lambda: one_finding.fingerprint(), 20000),
        "scoped_fingerprint": (lambda: one_finding.scoped_fingerprint("org/repo"), 20000),
        "classify_findings": (lambda: classify_findings("org/repo", previous, current), 2000),
        "codegraph_impact_of": (lambda: graph.impact_of(["pkg/mod0.py"]), 1000),
    }


def run(check: bool) -> tuple[list[dict], bool]:
    rows: list[dict] = []
    ok = True
    for name, (fn, number) in _benchmarks().items():
        # Warm up (imports, caches), then take the best of several repeats.
        fn()
        best = min(timeit.repeat(fn, number=number, repeat=5)) / number
        ns = best * 1e9
        budget = BUDGETS_NS.get(name, 0)
        breached = bool(check and budget and ns > budget)
        ok = ok and not breached
        rows.append(
            {
                "name": name,
                "ns_per_op": round(ns, 1),
                "ops_per_sec": round(1.0 / best) if best else 0,
                "budget_ns": budget,
                "breached": breached,
            }
        )
    return rows, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EvoAgent micro-benchmarks")
    parser.add_argument("--json", default="", help="write JSON results to this path")
    parser.add_argument("--check", action="store_true", help="fail if a budget is breached")
    args = parser.parse_args(argv)

    rows, ok = run(args.check)

    width = max(len(r["name"]) for r in rows)
    print("%-*s  %14s  %14s  %10s" % (width, "benchmark", "ns/op", "ops/sec", "budget"))
    for row in rows:
        flag = "  BREACH" if row["breached"] else ""
        print(
            "%-*s  %14.1f  %14d  %10s%s"
            % (
                width,
                row["name"],
                row["ns_per_op"],
                row["ops_per_sec"],
                row["budget_ns"] or "-",
                flag,
            )
        )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"benchmarks": rows}, handle, indent=2)

    if args.check and not ok:
        print("MICROBENCH REGRESSION: a budget was breached", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
