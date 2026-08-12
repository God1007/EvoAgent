import importlib.util
import os
import unittest

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

    def test_all_scenarios_have_valid_mix(self):
        for name in loadgen.SCENARIOS:
            plan = loadgen._weighted_plan(loadgen.SCENARIOS[name]["mix"])
            self.assertTrue(plan)


if __name__ == "__main__":
    unittest.main()
