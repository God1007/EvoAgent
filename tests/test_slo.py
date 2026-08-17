import json
import os
import tempfile
import unittest

from evoagent.slo import (
    PrometheusClient,
    SLOCatalog,
    SLOError,
    SLOObjective,
    evaluate_slos,
    load_slo_catalog,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class FakeResponse:
    def __init__(self, body, content_length=None):
        self.body = body
        self.headers = {
            "Content-Length": str(len(body) if content_length is None else content_length)
        }
        self.status = 200

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return self.response


class CatalogTests(unittest.TestCase):
    def test_repository_catalog_is_versioned_and_valid(self):
        catalog = load_slo_catalog(os.path.join(ROOT, "ops", "slo.toml"))

        self.assertEqual(1, catalog.version)
        self.assertEqual(
            {"api-availability", "async-intake-latency", "review-success"},
            {objective.objective_id for objective in catalog.objectives},
        )
        self.assertTrue(all("$window" in item.indicator_query for item in catalog.objectives))
        self.assertTrue(all("or vector(0)" in item.indicator_query for item in catalog.objectives))
        self.assertTrue(all("or vector(0)" in item.sample_query for item in catalog.objectives))

    def test_catalog_rejects_duplicate_ids_and_invalid_target(self):
        content = """version = 1
[[objectives]]
id = "same"
description = "one"
target = 1.0
window = "30d"
min_samples = 1
indicator_query = "one"
sample_query = "two"
[[objectives]]
id = "same"
description = "two"
target = 0.9
window = "30d"
min_samples = 1
indicator_query = "one"
sample_query = "two"
"""
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(content)
            path = handle.name
        self.addCleanup(os.unlink, path)

        with self.assertRaisesRegex(ValueError, "target"):
            load_slo_catalog(path)

    def test_prometheus_rules_and_dashboard_are_valid_operational_assets(self):
        with open(
            os.path.join(ROOT, "ops", "prometheus", "evoagent.rules.yml"),
            encoding="utf-8",
        ) as handle:
            rules = handle.read()
        with open(os.path.join(ROOT, "docs", "operations.md"), encoding="utf-8") as handle:
            runbook = handle.read().lower()
        with open(
            os.path.join(ROOT, "ops", "grafana", "evoagent-overview.json"),
            encoding="utf-8",
        ) as handle:
            dashboard = json.load(handle)

        self.assertIn("EvoAgentAvailabilityFastBurn", rules)
        self.assertIn("evoagent_queue_oldest_age_seconds", rules)
        self.assertIn("evoagent:cost:model_micros_per_terminal_review_30m", rules)
        self.assertIn("EvoAgentModelCapacitySaturated", rules)
        self.assertIn("EvoAgentRepairVerificationBlockedHigh", rules)
        self.assertIn("EvoAgentNegativeFeedbackHigh", rules)
        self.assertIn("EvoAgentRetentionMaintenanceStalled", rules)
        self.assertIn("or vector(0)", rules)
        for anchor in (
            "availability-fast-burn",
            "availability-slow-burn",
            "intake-latency",
            "queue-or-outbox-stale",
            "dead-letters",
            "review-failures",
            "model-route-capacity",
            "model-economics",
            "repair-outcomes",
            "quality-feedback",
            "plugin-runtime",
            "history-retention",
        ):
            self.assertIn("#" + anchor, rules)
            self.assertIn("## " + anchor.replace("-", " "), runbook)
        self.assertEqual("evoagent-enterprise", dashboard["uid"])
        self.assertGreaterEqual(len(dashboard["panels"]), 13)
        dashboard_text = json.dumps(dashboard)
        self.assertIn("evoagent:ratio:model_capacity_rejected_15m", dashboard_text)
        self.assertIn("evoagent:ratio:negative_feedback_24h", dashboard_text)
        self.assertIn("evoagent_retention_trace_events_pruned_total", dashboard_text)


class PrometheusClientTests(unittest.TestCase):
    def test_query_accepts_one_vector_value_and_sends_bearer_token(self):
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1, "0.9995"]}],
                },
            }
        ).encode()
        opener = FakeOpener(FakeResponse(body))
        client = PrometheusClient(
            "https://prometheus.example/base",
            ("prometheus.example",),
            bearer_token="test-token",
            opener=opener,
        )

        self.assertEqual(0.9995, client.query("up"))
        request, timeout = opener.requests[0]
        self.assertIn("/base/api/v1/query?", request.full_url)
        self.assertEqual("Bearer test-token", request.headers["Authorization"])
        self.assertEqual(10, timeout)

    def test_client_rejects_insecure_remote_and_ambiguous_results(self):
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            PrometheusClient("https://prometheus.example")
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            PrometheusClient("http://prometheus.example", ("prometheus.example",))

        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"pod": "a"}, "value": [1, "1"]},
                        {"metric": {"pod": "b"}, "value": [1, "1"]},
                    ],
                },
            }
        ).encode()
        client = PrometheusClient("http://127.0.0.1:9090", opener=FakeOpener(FakeResponse(body)))
        with self.assertRaisesRegex(SLOError, "exactly one"):
            client.query("up")

    def test_response_size_is_bounded(self):
        client = PrometheusClient(
            "http://127.0.0.1:9090",
            max_response_bytes=10,
            opener=FakeOpener(FakeResponse(b"{}", content_length=100)),
        )
        with self.assertRaisesRegex(SLOError, "byte limit"):
            client.query("up")


class EvaluationTests(unittest.TestCase):
    def test_evaluation_reports_pass_fail_and_error_budget(self):
        catalog = SLOCatalog(
            1,
            (
                SLOObjective(
                    "availability", "availability", 0.999, "30d", "good-$window", "n-$window", 10
                ),
                SLOObjective("reviews", "reviews", 0.99, "30d", "good-$window", "n-$window", 10),
            ),
        )

        class Client:
            values = iter((100.0, 0.9995, 100.0, 0.98))

            def query(self, _expression):
                return next(self.values)

        results = evaluate_slos(catalog, Client(), "7d")

        self.assertEqual(["pass", "fail"], [result.status for result in results])
        self.assertEqual(0.5, results[0].error_budget_remaining)
        self.assertEqual(-1.0, results[1].error_budget_remaining)
        self.assertTrue(all(result.window == "7d" for result in results))

    def test_insufficient_samples_does_not_claim_success(self):
        catalog = SLOCatalog(
            1,
            (SLOObjective("availability", "availability", 0.999, "30d", "good", "n", 10),),
        )

        class Client:
            @staticmethod
            def query(_expression):
                return 2.0

        result = evaluate_slos(catalog, Client())[0]
        self.assertEqual("no-data", result.status)
        self.assertIsNone(result.achieved)
        self.assertIsNone(result.error_budget_remaining)


if __name__ == "__main__":
    unittest.main()
