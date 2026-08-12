import importlib.util
import os
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "microbench",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "microbench.py"),
)
microbench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(microbench)


class MicrobenchTests(unittest.TestCase):
    def test_each_benchmark_callable_runs(self):
        # Fast smoke: call each hot-path benchmark once (not the timed loop).
        for name, (fn, _number) in microbench._benchmarks().items():
            result = fn()
            self.assertIsNotNone(result, name)

    def test_budgets_cover_all_benchmarks(self):
        names = set(microbench._benchmarks())
        self.assertEqual(names, set(microbench.BUDGETS_NS))


if __name__ == "__main__":
    unittest.main()
