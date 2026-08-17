import unittest

from evoagent.evaluation_metrics import confidence_calibration, languages_for_paths


class EvaluationMetricsTests(unittest.TestCase):
    def test_language_inference_is_normalized_and_multi_language(self):
        self.assertEqual(
            ["Other", "Python", "TypeScript", "YAML"],
            languages_for_paths(
                ["a/service/main.py", "b/web/App.TSX", "deploy/app.yaml", "README"]
            ),
        )

    def test_confidence_one_is_in_final_bin_and_invalid_values_are_rejected(self):
        results = [
            {
                "predictions": [
                    {"confidence": 1.0, "matched": True},
                    {"confidence": 0.0, "matched": False},
                    {"confidence": None, "matched": False},
                    {"confidence": 1.01, "matched": True},
                ]
            }
        ]

        report = confidence_calibration(results)

        self.assertEqual(4, report["predictions"])
        self.assertEqual(2, report["valid_confidences"])
        self.assertEqual(2, report["invalid_confidences"])
        self.assertEqual(1, report["bins"][0]["count"])
        self.assertEqual(1, report["bins"][9]["count"])
        self.assertEqual(0.0, report["expected_calibration_error"])
        self.assertEqual(0.0, report["brier_score"])


if __name__ == "__main__":
    unittest.main()
