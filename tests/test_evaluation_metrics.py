import unittest

from evoagent.evaluation_metrics import confidence_calibration, languages_for_paths, rule_slices


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
                    {"rule_id": "SEC-EVAL", "confidence": 1.0, "matched": True},
                    {"rule_id": "SEC-EVAL", "confidence": 0.0, "matched": False},
                    {"rule_id": "SEC-EVAL", "confidence": None, "matched": False},
                    {"rule_id": "SEC-EVAL", "confidence": 1.01, "matched": True},
                    {"rule_id": "SEC-EVAL", "confidence": True, "matched": True},
                    {"rule_id": "SEC-EVAL", "confidence": "0.9", "matched": True},
                    {"rule_id": "SEC-EVAL", "confidence": "bad", "matched": True},
                    {"rule_id": "SEC-EVAL", "confidence": float("nan"), "matched": True},
                    {"rule_id": "SEC-EVAL", "confidence": float("inf"), "matched": True},
                ]
            }
        ]

        report = confidence_calibration(results)

        self.assertEqual(9, report["predictions"])
        self.assertEqual(2, report["valid_confidences"])
        self.assertEqual(7, report["invalid_confidences"])
        self.assertEqual(1, report["bins"][0]["count"])
        self.assertEqual(1, report["bins"][9]["count"])
        self.assertEqual(0.0, report["expected_calibration_error"])
        self.assertEqual(0.0, report["brier_score"])
        self.assertEqual(0.5, rule_slices(results)["SEC-EVAL"]["mean_confidence"])


if __name__ == "__main__":
    unittest.main()
