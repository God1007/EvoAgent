import http.client
import io
import json
import os
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

from evoagent.api import ApiHandler
from evoagent.config import Settings
from evoagent.metrics import Metrics
from evoagent.migrations import CURRENT_SCHEMA_VERSION
from evoagent.service import ReviewService


class AdmissionControlTests(unittest.TestCase):
    def _serve(self, settings: Settings):
        service = ReviewService(settings)
        self.service = service
        handler = type(
            "ConfiguredApiHandler",
            (ApiHandler,),
            {"service": service, "settings": settings},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(service.close)
        self.addCleanup(server.shutdown)
        return server.server_address

    def _settings(self, **overrides) -> Settings:
        base = dict(
            host="127.0.0.1",
            port=0,
            db_path=os.path.join(tempfile.mkdtemp(), "evoagent.db"),
            max_diff_bytes=10000,
            max_steps=8,
            timeout_seconds=10,
            llm_base_url="",
            llm_api_key="",
            llm_model="",
            github_webhook_secret="",
            github_token="",
            auto_post_review=False,
            auth_required=False,
            redis_url="",
            async_workers=1,
        )
        base.update(overrides)
        return Settings(**base)

    # A non-probe endpoint (probes are intentionally exempt from rate limiting).
    _TASK = "/v1/tasks/00000000-0000-0000-0000-000000000000"

    def test_rate_limit_returns_429_with_retry_after(self):
        host, port = self._serve(self._settings(rate_limit_rps=1, rate_limit_burst=1))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", self._TASK)
        first = conn.getresponse()
        first.read()
        self.assertEqual(404, first.status)  # allowed through, task simply missing

        conn.request("GET", self._TASK)
        second = conn.getresponse()
        second.read()
        self.assertEqual(429, second.status)
        self.assertIsNotNone(second.getheader("Retry-After"))

    def test_tenant_review_capacity_returns_retryable_429_after_body_is_consumed(self):
        host, port = self._serve(
            self._settings(
                tenant_max_active_reviews=1,
                tenant_capacity_retry_seconds=7,
            )
        )
        self.service.store.create_review_task(
            "occupied",
            "org/occupied",
            1,
            {"source": "test"},
            "default",
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        payload = json.dumps(
            {
                "repository": "org/rejected",
                "diff": "--- a/a.py\n+++ b/a.py\n+value = 1\n",
                "pull_request": 2,
            }
        )

        conn.request(
            "POST",
            "/v1/reviews?async=true",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = json.loads(response.read())

        self.assertEqual(429, response.status)
        self.assertEqual("7", response.getheader("Retry-After"))
        self.assertEqual("tenant review capacity is exhausted", body["error"])
        self.assertEqual(1, self.service.store.tenant_review_admission_stats("default")["active"])
        audit = self.service.store.list_audit("default", 10)
        self.assertEqual("review.capacity-rejected", audit[0]["action"])

        conn.request("GET", "/api/tenant-review-capacity")
        capacity_response = conn.getresponse()
        capacity = json.loads(capacity_response.read())
        self.assertEqual(200, capacity_response.status)
        self.assertEqual(1, capacity["active_reviews"])
        self.assertTrue(capacity["saturated"])
        self.assertFalse(capacity["queue_fair_scheduling"])
        self.assertEqual(1, capacity["queue_weight"])
        self.assertEqual("uniform-v1", capacity["queue_policy_id"])

    def test_trusted_proxy_clients_receive_independent_rate_limit_buckets(self):
        host, port = self._serve(
            self._settings(
                rate_limit_rps=1,
                rate_limit_burst=1,
                trusted_proxy_cidrs=("127.0.0.1/32",),
            )
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        statuses = []
        for client in ("198.51.100.10", "198.51.100.11", "198.51.100.10"):
            conn.request("GET", self._TASK, headers={"X-Forwarded-For": client})
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)

        self.assertEqual([404, 404, 429], statuses)

    def test_untrusted_socket_peer_cannot_rotate_forwarded_rate_limit_keys(self):
        host, port = self._serve(
            self._settings(
                rate_limit_rps=1,
                rate_limit_burst=1,
                trusted_proxy_cidrs=("10.0.0.0/8",),
            )
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        statuses = []
        for client in ("198.51.100.10", "198.51.100.11"):
            conn.request("GET", self._TASK, headers={"X-Forwarded-For": client})
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)

        self.assertEqual([404, 429], statuses)

    def test_access_log_records_resolved_client_without_untrusted_left_prefix(self):
        host, port = self._serve(self._settings(trusted_proxy_cidrs=("127.0.0.1/32", "10.0.0.0/8")))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        output = io.StringIO()

        with redirect_stdout(output):
            conn.request(
                "GET",
                "/health",
                headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.20, 10.0.0.7"},
            )
            response = conn.getresponse()
            response.read()

        logs = output.getvalue()
        self.assertEqual(200, response.status)
        self.assertIn('"client": "198.51.100.20"', logs)
        self.assertIn('"peer": "127.0.0.1"', logs)
        self.assertIn('"client_source": "forwarded"', logs)
        self.assertIn('"forwarded_hops": 2', logs)
        self.assertNotIn("203.0.113.99", logs)

    def test_invalid_forwarded_chain_falls_back_to_peer_and_is_counted(self):
        host, port = self._serve(
            self._settings(
                rate_limit_rps=1,
                rate_limit_burst=1,
                trusted_proxy_cidrs=("127.0.0.1/32",),
            )
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        captured = Metrics()

        with mock.patch("evoagent.api.metrics", captured):
            statuses = []
            for invalid in ("proxy.internal", "198.51.100.8:443"):
                conn.request("GET", self._TASK, headers={"X-Forwarded-For": invalid})
                response = conn.getresponse()
                response.read()
                statuses.append(response.status)

        self.assertEqual([404, 429], statuses)
        self.assertIn(
            "evoagent_http_forwarded_invalid_total 2.0",
            captured.prometheus(),
        )

    def test_model_usage_reconciliation_http_boundary_is_state_guarded(self):
        host, port = self._serve(self._settings())
        request_id = "http-stale-request"
        self.service.store.reserve_model_usage(
            {
                "request_id": request_id,
                "tenant_id": "default",
                "repository": "org/repo",
                "purpose": "review",
                "provider": "provider",
                "model": "model",
                "reserved_tokens": 100,
                "request_sha256": "a" * 64,
                "created_at": "2000-01-01T00:00:00+00:00",
            },
            "2000-01-01T00:00:00+00:00",
        )
        self.service.store.expire_model_usage_reservations("2001-01-01T00:00:00+00:00")
        payload = json.dumps(
            {
                "request_id": request_id,
                "status": "success",
                "input_tokens": 12,
                "output_tokens": 3,
                "cost_micros": 7,
            }
        )
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request(
            "POST",
            "/v1/model-usage/reconcile",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        first = conn.getresponse()
        first_body = json.loads(first.read())
        self.assertEqual(200, first.status)
        self.assertTrue(first_body["reconciled"])

        conn.request(
            "POST",
            "/v1/model-usage/reconcile",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        second = conn.getresponse()
        second_body = json.loads(second.read())
        self.assertEqual(404, second.status)
        self.assertFalse(second_body["reconciled"])

    def test_probes_are_exempt_from_rate_limiting(self):
        host, port = self._serve(self._settings(rate_limit_rps=1, rate_limit_burst=1))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(10):
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read()
            self.assertEqual(200, response.status)

    def test_health_and_inventory_report_plugin_runtime(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/health")
        health_response = conn.getresponse()
        health = json.loads(health_response.read())
        self.assertEqual("running", health["plugin_runtime"])
        self.assertGreaterEqual(health["plugins"], 10)
        self.assertFalse(health["retention"]["enabled"])
        self.assertFalse(health["review_admission"]["enabled"])

        conn.request("GET", "/v1/plugins")
        inventory_response = conn.getresponse()
        inventory = json.loads(inventory_response.read())
        self.assertEqual(200, inventory_response.status)
        self.assertIn("store", inventory["capabilities"])

        conn.request("GET", "/ready")
        ready_response = conn.getresponse()
        ready = json.loads(ready_response.read())
        self.assertEqual(200, ready_response.status)
        self.assertEqual(CURRENT_SCHEMA_VERSION, ready["checks"]["schema_version"])
        self.assertFalse(ready["checks"]["queue"]["fair_scheduling"])
        self.assertEqual("uniform-v1", ready["checks"]["queue"]["fair_policy_id"])

    def test_request_identity_and_security_headers_cover_every_response(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/health", headers={"X-Request-ID": "gateway-request.42"})
        accepted = conn.getresponse()
        accepted.read()
        self.assertEqual("gateway-request.42", accepted.getheader("X-Request-ID"))
        self.assertEqual("nosniff", accepted.getheader("X-Content-Type-Options"))
        self.assertEqual("DENY", accepted.getheader("X-Frame-Options"))
        self.assertEqual("same-origin", accepted.getheader("Referrer-Policy"))
        self.assertIn("camera=()", accepted.getheader("Permissions-Policy", ""))
        self.assertNotIn("Python", accepted.getheader("Server", ""))

        conn.request("GET", "/health", headers={"X-Request-ID": "invalid request id"})
        replaced = conn.getresponse()
        replaced.read()
        generated = replaced.getheader("X-Request-ID", "")
        self.assertNotEqual("invalid request id", generated)
        self.assertRegex(generated, re.compile(r"^[0-9a-f]{32}$"))

    def test_internal_error_is_correlated_without_leaking_exception_or_query(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        def fail_inventory():
            raise RuntimeError("password=provider-secret-value")

        self.service.plugin_status = fail_inventory
        output = io.StringIO()
        with redirect_stdout(output):
            conn.request(
                "GET",
                "/v1/plugins?access_token=query-secret-value",
                headers={"X-Request-ID": "edge-error-17"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read())

        logs = output.getvalue()
        self.assertEqual(500, response.status)
        self.assertEqual("edge-error-17", response.getheader("X-Request-ID"))
        self.assertEqual({"error": "internal server error", "request_id": "edge-error-17"}, payload)
        self.assertNotIn("provider-secret-value", json.dumps(payload))
        self.assertNotIn("provider-secret-value", logs)
        self.assertNotIn("query-secret-value", logs)
        self.assertIn('"event": "http_internal_error"', logs)
        self.assertIn('"error_type": "builtins.RuntimeError"', logs)
        self.assertIn('"path": "/v1/plugins"', logs)

    def test_post_internal_error_uses_the_same_safe_boundary(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        def fail_reconciliation(*_args):
            # Built-in ValueError is not assumed to be public-safe. Only the
            # explicit ClientInputError marker may cross the HTTP boundary.
            raise ValueError("postgresql://admin:database-secret@db/reviews")

        self.service.reconcile_model_usage = fail_reconciliation
        body = json.dumps(
            {
                "request_id": "provider-request",
                "status": "success",
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_micros": 1,
            }
        )
        output = io.StringIO()
        with redirect_stdout(output):
            conn.request(
                "POST",
                "/v1/model-usage/reconcile",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Request-ID": "post-error-18",
                },
            )
            response = conn.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(500, response.status)
        self.assertEqual({"error": "internal server error", "request_id": "post-error-18"}, payload)
        self.assertNotIn("database-secret", output.getvalue())

    def test_list_limit_is_bounded_and_returns_a_reviewed_client_error(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/api/tasks?limit=1001")
        response = conn.getresponse()
        payload = json.loads(response.read())

        self.assertEqual(400, response.status)
        self.assertEqual({"error": "limit must be an integer between 1 and 1000"}, payload)

    def test_repository_policy_api_versions_and_reads_tenant_policy(self):
        host, port = self._serve(self._settings())
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        body = json.dumps(
            {
                "repository": "org/repo",
                "policy": {
                    "auto_fix": True,
                    "allowed_fix_rules": ["SEC-YAML-LOAD"],
                    "max_diff_bytes": 4096,
                },
            }
        )
        conn.request(
            "POST",
            "/v1/repository-policies",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        created_response = conn.getresponse()
        created = json.loads(created_response.read())
        self.assertEqual(201, created_response.status)
        self.assertEqual(1, created["version"])

        conn.request("GET", "/v1/repository-policies?repository=org%2Frepo")
        fetched_response = conn.getresponse()
        fetched = json.loads(fetched_response.read())
        self.assertEqual(200, fetched_response.status)
        self.assertEqual("configured", fetched["source"])
        self.assertEqual(4096, fetched["policy"]["max_diff_bytes"])
        self.assertEqual([1], [item["version"] for item in fetched["history"]])

    def test_model_usage_api_is_tenant_scoped_operational_metadata(self):
        host, port = self._serve(self._settings())
        request_id = "model-request-1"
        self.service.store.reserve_model_usage(
            {
                "request_id": request_id,
                "tenant_id": "default",
                "repository": "org/repo",
                "task_id": "task-1",
                "purpose": "review",
                "provider": "test",
                "model": "model-a",
                "reserved_tokens": 20,
                "reserved_cost_micros": 4,
                "redactions": 1,
                "request_sha256": "a" * 64,
                "created_at": "2026-08-17T00:00:00+00:00",
            },
            "2026-08-17T00:00:00+00:00",
        )
        self.service.store.complete_model_usage(request_id, "success", 10, 3, 2)

        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/model-usage?repository=org%2Frepo")
        response = conn.getresponse()
        payload = json.loads(response.read())

        self.assertEqual(200, response.status)
        self.assertEqual([request_id], [item["request_id"] for item in payload["usage"]])
        self.assertNotIn("messages", payload["usage"][0])

    def test_model_route_promotion_report_http_boundary(self):
        host, port = self._serve(self._settings())
        expected = {
            "candidate_route_id": "candidate",
            "eligible": False,
            "checks": {"minimum_samples": False},
        }
        with mock.patch.object(
            self.service.model_gateway, "promotion_report", return_value=expected
        ) as report:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            self.addCleanup(conn.close)
            conn.request(
                "GET",
                "/api/model-routes/promotion?route_id=candidate&repository=org%2Frepo",
            )
            response = conn.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(200, response.status)
        self.assertEqual(expected, payload)
        report.assert_called_once_with("default", "candidate", "org/repo")

        conn.request("GET", "/api/model-routes/promotion")
        missing = conn.getresponse()
        missing_payload = json.loads(missing.read())
        self.assertEqual(400, missing.status)
        self.assertIn("route_id", missing_payload["error"])

    def test_model_route_capacity_report_http_boundary(self):
        host, port = self._serve(self._settings())
        expected = {
            "topology_sha256": "a" * 64,
            "routes": [{"route_id": "primary", "available": True}],
        }
        with mock.patch.object(
            self.service.model_gateway, "capacity_report", return_value=expected
        ) as report:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            self.addCleanup(conn.close)
            conn.request("GET", "/api/model-routes/capacity?repository=org%2Frepo")
            response = conn.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(200, response.status)
        self.assertEqual(expected, payload)
        report.assert_called_once_with("default", "org/repo")

    def test_rejected_requests_are_counted_in_metrics(self):
        host, port = self._serve(self._settings(rate_limit_rps=1, rate_limit_burst=1))
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(3):
            conn.request("GET", self._TASK)
            conn.getresponse().read()

        conn.request("GET", "/metrics")
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        self.assertIn("evoagent_http_rejected_total", body)
        self.assertIn("evoagent_http_in_flight", body)
        self.assertIn("evoagent_http_requests_total", body)
        self.assertIn("evoagent_http_read_responses_4xx_total", body)
        self.assertIn("evoagent_http_request_read_count", body)

    def test_sync_review_shed_but_async_exempt_when_heavy_gate_full(self):
        host, port = self._serve(self._settings(max_inflight_heavy=1))
        # Occupy the only heavy slot so the gate is full for the next request.
        self.assertTrue(self.service.heavy_gate.try_acquire())
        self.addCleanup(self.service.heavy_gate.release)

        payload = (
            '{"repository": "org/repo", "diff": "--- a/a\\n+++ b/a\\n@@ -1 +1 @@\\n-x\\n+y\\n"}'
        )
        headers = {"Content-Type": "application/json"}

        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("POST", "/v1/reviews", body=payload, headers=headers)
        sync = conn.getresponse()
        sync.read()
        self.assertEqual(503, sync.status)  # synchronous heavy work is shed
        self.assertIsNotNone(sync.getheader("Retry-After"))

        conn.request("POST", "/v1/reviews?async=true", body=payload, headers=headers)
        async_response = conn.getresponse()
        async_response.read()
        self.assertNotEqual(503, async_response.status)  # async intake is exempt

    def test_disabled_rate_limit_allows_burst(self):
        host, port = self._serve(self._settings())  # rate_limit_rps defaults to 0
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(20):
            conn.request("GET", self._TASK)
            response = conn.getresponse()
            response.read()
            self.assertEqual(404, response.status)


if __name__ == "__main__":
    unittest.main()
