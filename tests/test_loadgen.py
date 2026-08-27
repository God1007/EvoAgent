import importlib.util
import io
import math
import os
import queue
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "loadgen",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "loadgen.py"),
)
loadgen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(loadgen)


class PercentileTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(10.0, loadgen._percentile(values, 0))
        self.assertEqual(40.0, loadgen._percentile(values, 100))
        self.assertAlmostEqual(25.0, loadgen._percentile(values, 50))

    def test_percentile_empty(self):
        self.assertEqual(0.0, loadgen._percentile([], 99))


class PlanTests(unittest.TestCase):
    def test_weights_expand_proportionally(self):
        mix = [("GET", "/a", 3, None), ("GET", "/b", 1, None)]
        plan = loadgen._weighted_plan(mix)
        paths = [p for _m, p, _b in plan]
        self.assertEqual(3, paths.count("/a"))
        self.assertEqual(1, paths.count("/b"))

    def test_intake_warmup_does_not_enqueue_background_work(self):
        self.assertEqual("steady", loadgen._warmup_scenario("intake"))

    def test_read_only_warmup_preserves_the_selected_scenario(self):
        self.assertEqual("stress-ramp", loadgen._warmup_scenario("stress-ramp"))

    def test_arrival_times_survive_rate_changes_and_scheduler_lateness(self):
        clock = SimpleNamespace(now=0.0, delayed=False)

        def sleep(seconds):
            clock.now += seconds
            if not clock.delayed:
                clock.now += 2.5
                clock.delayed = True

        timing = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
        for scenario, count, first_times in (
            ("steady", 100, [0.0, 0.1, 0.2]),
            ("spike", 82, [0.0, 1.0, 2.0, 2.1]),
            ("stress-ramp", 50, [0.0, math.sqrt(2), 2.0]),
        ):
            with (
                self.subTest(scenario=scenario),
                mock.patch.object(loadgen, "time", timing),
                mock.patch.object(loadgen._Worker, "_connect", return_value=mock.Mock()),
                mock.patch.object(loadgen._Worker, "_issue", return_value=(200, False)),
            ):
                clock.now, clock.delayed = 0.0, False
                results = loadgen.generate("http://localhost", scenario, 10, 10, 1, {}, 1)
                scheduled = sorted(sample.scheduled for sample in results.samples)
                self.assertEqual(count, results.sent)
                self.assertEqual(count, results.completed)
                for expected, actual in zip(first_times, scheduled, strict=False):
                    self.assertAlmostEqual(expected, actual)
                self.assertGreaterEqual(max(sample.latency for sample in results.samples), 2.5)

        jobs = mock.Mock()

        def full(job, **_kwargs):
            if job is not None:
                raise queue.Full

        jobs.put.side_effect = jobs.put_nowait.side_effect = full
        with (
            mock.patch.object(loadgen, "time", timing),
            mock.patch.object(loadgen, "_Worker"),
            mock.patch.object(loadgen.queue, "Queue", return_value=jobs),
        ):
            clock.now, clock.delayed = 0.0, False
            results = loadgen.generate("http://localhost", "steady", 10, 10, 1, {}, 1)
        summary = loadgen.summarize(results, 10)
        self.assertEqual(100, summary["sent"])
        self.assertEqual(0, summary["completed"])
        self.assertEqual(100, summary["dropped"])
        self.assertEqual(1.0, summary["error_rate"])
        self.assertEqual({"0": 100}, summary["status_counts"])
        self.assertEqual([], results.samples)


class SummarizeTests(unittest.TestCase):
    def test_summary_computes_rates_and_percentiles(self):
        results = loadgen.Results()
        for i in range(100):
            error = i >= 98  # 2 errors
            status = 500 if error else 200
            results.samples.append(loadgen.Sample(0.0, 0.05, status, error))
            results.completed += 1
            results.sent += 1
            if error:
                results.errors += 1
            results.status_counts[status] = results.status_counts.get(status, 0) + 1
        summary = loadgen.summarize(results, duration=10.0)
        self.assertEqual(100, summary["completed"])
        self.assertEqual(2, summary["errors"])
        self.assertAlmostEqual(0.02, summary["error_rate"], places=4)
        self.assertEqual(10.0, summary["throughput_rps"])
        self.assertAlmostEqual(50.0, summary["latency_ms"]["p50"], places=1)

        results.sent += 5
        results.errors += 5
        results.dropped = 5
        results.elapsed_seconds = 20.0
        for sample in results.samples:
            sample.latency = 0.050001
        summary = loadgen.summarize(results, duration=10.0)
        self.assertEqual(7 / 105, summary["error_rate"])
        self.assertEqual(5, summary["dropped"])
        self.assertEqual(5.0, summary["throughput_rps"])
        self.assertGreater(summary["latency_ms"]["p99"], 50.0)

    def test_all_scenarios_have_valid_mix(self):
        for name in loadgen.SCENARIOS:
            plan = loadgen._weighted_plan(loadgen.SCENARIOS[name]["mix"])
            self.assertTrue(plan)

    def test_rejected_http_work_cannot_pass_the_error_gate(self):
        worker = loadgen._Worker("http://localhost", None, None, None, {}, 1)
        conn = mock.Mock()
        for status in (200, 202, 302, 401, 403, 429, 503):
            with self.subTest(status=status):
                conn.getresponse.return_value.status = status
                self.assertEqual(
                    (status, not 200 <= status < 300), worker._issue(conn, "GET", "/ready", None)
                )

    def test_invalid_cli_limits_fail_before_any_warmup_or_request(self):
        with (
            mock.patch.object(loadgen, "generate", return_value=loadgen.Results()) as generate,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            for flag, value in (
                ("--duration", "0"),
                ("--duration", "nan"),
                ("--rate", "-1"),
                ("--rate", "inf"),
                ("--concurrency", "0"),
                ("--timeout", "0"),
                ("--warmup", "-1"),
                ("--p99-ms", "nan"),
                ("--max-error-rate", "1.1"),
                ("--base-url", "ftp://localhost"),
                ("--base-url", "http://localhost:bad"),
            ):
                with self.subTest(flag=flag, value=value):
                    with self.assertRaises(SystemExit) as error:
                        loadgen.main(["--warmup", "0", flag, value])
                    self.assertEqual(2, error.exception.code)
            generate.assert_not_called()
            self.assertEqual(1, loadgen.main(["--warmup", "0"]))


@unittest.skipUnless(os.name == "posix", "the baseline runner is a POSIX shell script")
class BaselineProcessSafetyTests(unittest.TestCase):
    def test_occupied_port_is_rejected_without_signalling_its_owner(self):
        listener = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-c",
                "from http.server import HTTPServer, BaseHTTPRequestHandler\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                " def do_GET(self): self.send_response(503); self.end_headers()\n"
                " def log_message(self, *args): pass\n"
                "server=HTTPServer(('127.0.0.1',0), Handler)\n"
                "print(server.server_port, flush=True)\n"
                "server.serve_forever()\n",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(listener.wait, timeout=5)
        self.addCleanup(listener.terminate)
        self.addCleanup(listener.stdout.close)
        port = listener.stdout.readline().strip()
        self.assertTrue(port.isdigit())
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            # Record attempted signals without letting a regressed runner kill anything.
            guarded = (
                'kill() { if [ "${1-}" = "-0" ]; then builtin kill "$@"; '
                'else printf "%s\\n" "$*" >> "$EVOAGENT_TEST_SIGNAL_LOG"; return 1; fi; }; '
                'export -f kill; exec bash "$@"'
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    guarded,
                    "guarded-baseline",
                    str(root / "scripts/perf_baseline.sh"),
                    "single",
                    "--quick",
                    "--out",
                    directory,
                ],
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHON": sys.executable,
                    "EVOAGENT_DATABASE_URL": "postgresql://127.0.0.1:1/unused",
                    "EVOAGENT_PERF_PORT": port,
                    "EVOAGENT_TEST_SIGNAL_LOG": str(Path(directory) / "signals.txt"),
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(0, result.returncode)
            signals = Path(directory) / "signals.txt"
            self.assertFalse(signals.exists(), signals.read_text() if signals.exists() else "")
            self.assertIn("unavailable", result.stderr)
            self.assertFalse((Path(directory) / "server.log").exists())
            self.assertIsNone(listener.poll())


if __name__ == "__main__":
    unittest.main()
