import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from evoagent.api import ApiHandler
from evoagent.config import Settings
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
        self.addCleanup(service.queue.close)
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
