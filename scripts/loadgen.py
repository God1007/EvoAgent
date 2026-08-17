#!/usr/bin/env python3
"""Open-model, constant-arrival-rate HTTP load generator for EvoAgent.

Why not a simple loop? A closed-loop client (send, wait for response, send next)
hides latency during stalls - the classic *coordinated omission* problem. This
generator schedules request send times on a fixed cadence independent of when
responses arrive, and measures each request's latency against its *scheduled*
send time. That surfaces the tail latency a real user population would feel.

Pure stdlib (threads + http.client), so it runs anywhere Python does - offline,
in CI, no k6/wrk required. Exits non-zero when a threshold is breached, so it
doubles as a perf-regression gate.

Examples:
    python scripts/loadgen.py --scenario steady --duration 30 --rate 200
    python scripts/loadgen.py --scenario stress-ramp --duration 60 --rate 500 \
        --p99-ms 150 --max-error-rate 0.001 --json out.json
"""

import argparse
import http.client
import json
import queue
import statistics
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

# Endpoint mixes per scenario. Each entry is (method, path, weight, body).
# Weights are relative; the generator picks endpoints proportionally.
_READ_MIX = [
    ("GET", "/health", 3, None),
    ("GET", "/ready", 1, None),
    ("GET", "/metrics", 1, None),
]
_INTAKE_MIX = [
    ("GET", "/health", 1, None),
    (
        "POST",
        "/v1/reviews?async=true",
        3,
        {"repository": "org/repo", "diff": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"},
    ),
]

SCENARIOS: dict[str, dict] = {
    # name: {mix, ramp: bool (ramp arrival rate 0->rate over duration)}
    "smoke": {"mix": _READ_MIX, "ramp": False},
    "steady": {"mix": _READ_MIX, "ramp": False},
    "intake": {"mix": _INTAKE_MIX, "ramp": False},
    "stress-ramp": {"mix": _READ_MIX, "ramp": True},
    "spike": {"mix": _READ_MIX, "ramp": False, "spike": True},
    "soak": {"mix": _READ_MIX, "ramp": False},
}


@dataclass
class Sample:
    scheduled: float
    latency: float  # seconds, measured from scheduled send time
    status: int
    error: bool


@dataclass
class Results:
    samples: list[Sample] = field(default_factory=list)
    sent: int = 0
    completed: int = 0
    errors: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = pct / 100.0 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _weighted_plan(mix: list) -> list:
    plan = []
    for method, path, weight, body in mix:
        for _ in range(max(1, int(weight))):
            plan.append((method, path, body))
    return plan


def _warmup_scenario(scenario: str) -> str:
    """Warm stateful intake tests without creating a measured-work backlog."""
    return "steady" if scenario == "intake" else scenario


class _Worker(threading.Thread):
    def __init__(
        self,
        base_url: str,
        jobs: "queue.Queue",
        results: Results,
        lock: threading.Lock,
        headers: dict,
        timeout: float,
    ):
        super().__init__(daemon=True)
        self._parsed = urllib.parse.urlparse(base_url)
        self._jobs = jobs
        self._results = results
        self._lock = lock
        self._headers = headers
        self._timeout = timeout

    def _connect(self):
        if self._parsed.scheme == "https":
            return http.client.HTTPSConnection(
                self._parsed.hostname, self._parsed.port, timeout=self._timeout
            )
        return http.client.HTTPConnection(
            self._parsed.hostname, self._parsed.port, timeout=self._timeout
        )

    def run(self) -> None:
        conn = self._connect()
        while True:
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                break
            scheduled, method, path, body = job
            status, error = self._issue(conn, method, path, body)
            if error:
                conn.close()
                conn = self._connect()
            latency = time.monotonic() - scheduled
            with self._lock:
                self._results.samples.append(Sample(scheduled, latency, status, error))
                self._results.completed += 1
                if error:
                    self._results.errors += 1
                self._results.status_counts[status] = self._results.status_counts.get(status, 0) + 1
            self._jobs.task_done()

    def _issue(self, conn, method: str, path: str, body):
        payload = None
        headers = dict(self._headers)
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            response.read()
            # 5xx and 429/503 count as errors for SLO purposes.
            error = response.status >= 500 or response.status in (429, 503)
            return response.status, error
        except Exception:
            return 0, True


def generate(
    base_url: str,
    scenario: str,
    duration: float,
    rate: float,
    concurrency: int,
    headers: dict,
    timeout: float,
) -> Results:
    config = SCENARIOS[scenario]
    plan = _weighted_plan(config["mix"])
    results = Results()
    lock = threading.Lock()
    jobs: queue.Queue = queue.Queue(maxsize=concurrency * 100)

    workers = [_Worker(base_url, jobs, results, lock, headers, timeout) for _ in range(concurrency)]
    for worker in workers:
        worker.start()

    start = time.monotonic()
    end = start + duration
    index = 0
    while True:
        now = time.monotonic()
        if now >= end:
            break
        # Effective arrival rate (constant, ramped, or spiking).
        if config.get("ramp"):
            current_rate = max(1.0, rate * (now - start) / duration)
        elif config.get("spike"):
            # First 20% at 10% rate, then jump to full rate.
            current_rate = rate if (now - start) > 0.2 * duration else max(1.0, rate * 0.1)
        else:
            current_rate = rate
        interval = 1.0 / current_rate
        method, path, body = plan[index % len(plan)]
        index += 1
        scheduled = time.monotonic()
        try:
            jobs.put((scheduled, method, path, body), timeout=1.0)
            with lock:
                results.sent += 1
        except queue.Full:
            # Backlog: the system under test cannot keep up. Record a dropped
            # sample so this shows up as latency/error rather than being omitted.
            with lock:
                results.samples.append(Sample(scheduled, timeout, 0, True))
                results.sent += 1
                results.errors += 1
        # Sleep until the next scheduled send (coordinated-omission-safe cadence).
        target = start + index * interval
        sleep_for = target - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)

    jobs.join()
    for _ in workers:
        jobs.put(None)
    return results


def summarize(results: Results, duration: float) -> dict:
    latencies = [s.latency * 1000.0 for s in results.samples]  # ms
    wall = duration if duration > 0 else 1.0
    error_rate = (results.errors / results.completed) if results.completed else 0.0
    return {
        "sent": results.sent,
        "completed": results.completed,
        "errors": results.errors,
        "error_rate": round(error_rate, 6),
        "throughput_rps": round(results.completed / wall, 2),
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 2),
            "p95": round(_percentile(latencies, 95), 2),
            "p99": round(_percentile(latencies, 99), 2),
            "p99_9": round(_percentile(latencies, 99.9), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
        },
        "status_counts": {str(k): v for k, v in sorted(results.status_counts.items())},
    }


def _render_text(summary: dict, scenario: str, rate: float) -> str:
    lat = summary["latency_ms"]
    return "\n".join(
        [
            "EvoAgent load test - scenario=%s target_rate=%.0f/s" % (scenario, rate),
            "  requests: sent=%d completed=%d errors=%d (%.3f%%)"
            % (
                summary["sent"],
                summary["completed"],
                summary["errors"],
                summary["error_rate"] * 100,
            ),
            "  throughput: %.1f req/s" % summary["throughput_rps"],
            "  latency ms: p50=%.1f p95=%.1f p99=%.1f p99.9=%.1f max=%.1f"
            % (lat["p50"], lat["p95"], lat["p99"], lat["p99_9"], lat["max"]),
            "  status: %s" % summary["status_counts"],
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EvoAgent constant-arrival-rate load generator")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--scenario", default="steady", choices=sorted(SCENARIOS))
    parser.add_argument("--duration", type=float, default=30.0, help="seconds")
    parser.add_argument("--rate", type=float, default=200.0, help="target requests/second")
    parser.add_argument("--concurrency", type=int, default=32, help="worker connections")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--token", default="", help="Bearer token for authenticated runs")
    parser.add_argument(
        "--warmup", type=float, default=2.0, help="seconds discarded before measuring"
    )
    parser.add_argument(
        "--p99-ms", type=float, default=0.0, help="fail if p99 exceeds this (0=off)"
    )
    parser.add_argument("--max-error-rate", type=float, default=1.0, help="fail if exceeded")
    parser.add_argument("--json", default="", help="write JSON summary to this path")
    args = parser.parse_args(argv)

    headers = {"Accept": "application/json"}
    if args.token:
        headers["Authorization"] = "Bearer " + args.token

    if args.warmup > 0:
        generate(
            args.base_url,
            _warmup_scenario(args.scenario),
            args.warmup,
            args.rate,
            args.concurrency,
            headers,
            args.timeout,
        )

    results = generate(
        args.base_url,
        args.scenario,
        args.duration,
        args.rate,
        args.concurrency,
        headers,
        args.timeout,
    )
    summary = summarize(results, args.duration)
    print(_render_text(summary, args.scenario, args.rate))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {"scenario": args.scenario, "target_rate": args.rate, **summary}, handle, indent=2
            )

    breached = []
    if args.p99_ms and summary["latency_ms"]["p99"] > args.p99_ms:
        breached.append("p99 %.1fms > %.1fms" % (summary["latency_ms"]["p99"], args.p99_ms))
    if summary["error_rate"] > args.max_error_rate:
        breached.append("error_rate %.4f > %.4f" % (summary["error_rate"], args.max_error_rate))
    if breached:
        print("THRESHOLD BREACH: " + "; ".join(breached), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
