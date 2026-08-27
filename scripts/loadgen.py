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
import math
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
    dropped: int = 0
    elapsed_seconds: float = 0.0
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
                conn.close()
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
            # Redirects and rejected/auth-failed work are not successful measurements.
            error = not 200 <= response.status < 300
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
    index = 0
    while True:
        # Invert cumulative arrivals. A rate change must not reschedule prior slots.
        if config.get("ramp"):
            offset = math.sqrt(2 * index / (rate * duration)) * duration
        elif config.get("spike"):
            low_duration, low_rate = 0.2 * duration, 0.1 * rate
            low_count = low_duration * low_rate
            offset = (
                index / low_rate if index < low_count else low_duration + (index - low_count) / rate
            )
        else:
            offset = index / rate
        scheduled = start + min(offset, duration)
        sleep_for = scheduled - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        if offset >= duration:
            break
        method, path, body = plan[index % len(plan)]
        index += 1
        with lock:
            results.sent += 1
        try:
            jobs.put_nowait((scheduled, method, path, body))
        except queue.Full:
            # Never slow the arrival clock to match worker capacity. Drops count
            # against the error budget, but have no fabricated HTTP latency sample.
            with lock:
                results.dropped += 1
                results.errors += 1
                results.status_counts[0] = results.status_counts.get(0, 0) + 1

    jobs.join()
    for _ in workers:
        jobs.put(None)
    for worker in workers:
        worker.join()
    results.elapsed_seconds = time.monotonic() - start
    return results


def summarize(results: Results, duration: float) -> dict:
    latencies = [s.latency * 1000.0 for s in results.samples]  # ms
    wall = results.elapsed_seconds or (duration if duration > 0 else 1.0)
    error_rate = results.errors / results.sent if results.sent else 0.0
    return {
        "sent": results.sent,
        "completed": results.completed,
        "errors": results.errors,
        "dropped": results.dropped,
        "elapsed_seconds": wall,
        "error_rate": error_rate,
        "throughput_rps": results.completed / wall,
        "latency_ms": {
            "p50": _percentile(latencies, 50),
            "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99),
            "p99_9": _percentile(latencies, 99.9),
            "max": max(latencies) if latencies else 0.0,
            "mean": statistics.fmean(latencies) if latencies else 0.0,
        },
        "status_counts": {str(k): v for k, v in sorted(results.status_counts.items())},
    }


def _render_text(summary: dict, scenario: str, rate: float) -> str:
    lat = summary["latency_ms"]
    return "\n".join(
        [
            "EvoAgent load test - scenario=%s target_rate=%.0f/s" % (scenario, rate),
            "  requests: offered=%d completed=%d dropped=%d errors=%d (%.3f%%)"
            % (
                summary["sent"],
                summary["completed"],
                summary["dropped"],
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

    if (
        any(
            not math.isfinite(value) or value <= 0
            for value in (args.duration, args.rate, args.timeout, args.duration * args.rate)
        )
        or args.concurrency <= 0
        or any(not math.isfinite(value) or value < 0 for value in (args.warmup, args.p99_ms))
        or not math.isfinite(args.warmup * args.rate)
        or not math.isfinite(args.max_error_rate)
        or not 0 <= args.max_error_rate <= 1
    ):
        parser.error(
            "duration/rate/timeout/concurrency must be positive; warmup/p99 nonnegative; "
            "error rate in [0,1]; numeric limits finite"
        )
    try:
        target = urllib.parse.urlsplit(args.base_url)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.path not in {"", "/"}
            or target.query
            or target.fragment
            or target.port == 0
        ):
            raise ValueError("invalid origin")
        # Validate the host/port without opening a socket or starting worker threads.
        http.client.HTTPConnection(target.hostname, target.port).close()
    except (ValueError, http.client.InvalidURL):
        parser.error("base-url must be a valid HTTP(S) origin without credentials or a path")

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
    if not results.completed:
        breached.append("no HTTP requests completed")
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
