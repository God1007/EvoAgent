import http.client
import socket
import threading
import unittest
from unittest import mock

from evoagent import api
from evoagent.api import DRAINING, _make_server
from evoagent.config import Settings
from evoagent.service import ReviewService
from tests.db_support import postgres_url, reset_postgres


def _settings(database_url="", **overrides) -> Settings:
    base = dict(
        host="127.0.0.1",
        port=0,
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
        database_url=database_url,
    )
    base.update(overrides)
    return Settings(**base)


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        DRAINING.clear()
        self.addCleanup(DRAINING.clear)
        database_url = postgres_url(self)
        reset_postgres(database_url)
        self.service = ReviewService(_settings(database_url, port=8080))
        self.addCleanup(self.service.close)
        self.server = _make_server(_settings(database_url), self.service)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host, self.port = self.server.server_address

    def _get(self, path):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_ready_when_dependencies_reachable(self):
        status, _ = self._get("/ready")
        self.assertEqual(200, status)

    def test_ready_returns_503_while_draining(self):
        DRAINING.set()
        status, body = self._get("/ready")
        self.assertEqual(503, status)
        self.assertIn(b"draining", body)

    def test_health_is_liveness_independent_of_draining(self):
        DRAINING.set()
        status, _ = self._get("/health")
        self.assertEqual(200, status)  # liveness stays up during drain


class LifecycleTests(unittest.TestCase):
    def test_bind_failure_closes_started_service(self):
        service = mock.Mock()
        settings = mock.Mock()
        with (
            mock.patch.object(api.Settings, "from_env", return_value=settings),
            mock.patch.object(api, "ReviewService", return_value=service),
            mock.patch.object(api, "_make_server", side_effect=OSError("address in use")),
            mock.patch.object(api, "_print_banner") as banner,
            self.assertRaisesRegex(OSError, "address in use"),
        ):
            api.run()

        service.close.assert_called_once_with()
        banner.assert_not_called()

    def test_http_handlers_finish_before_service_closes(self):
        service = mock.Mock()
        server = mock.Mock()
        settings = mock.Mock(shutdown_grace_seconds=2.5)
        order = mock.Mock()
        order.attach_mock(server.server_close, "server_close")
        order.attach_mock(service.close, "service_close")

        with (
            mock.patch.object(api.Settings, "from_env", return_value=settings),
            mock.patch.object(api, "ReviewService", return_value=service),
            mock.patch.object(api, "_make_server", return_value=server),
            mock.patch.object(api, "_print_banner"),
            mock.patch.object(api, "_install_drain_on_sigterm") as install_drain,
        ):
            api.run()

        install_drain.assert_called_once_with(server, 2.5)
        self.assertEqual([mock.call.server_close(), mock.call.service_close()], order.mock_calls)


class ConnectionLimitTests(unittest.TestCase):
    def test_releases_capacity_when_spawning_handler_fails(self):
        server = _make_server(_settings(max_http_connections=1), mock.Mock())
        self.addCleanup(server.server_close)
        request = mock.Mock(spec=socket.socket)
        address = ("127.0.0.1", 1)

        with (
            mock.patch.object(
                api.ThreadingHTTPServer,
                "process_request",
                side_effect=RuntimeError("cannot spawn handler"),
            ),
            mock.patch.object(api.metrics, "add_gauge") as gauge,
            self.assertRaisesRegex(RuntimeError, "cannot spawn handler"),
        ):
            server.process_request(request, address)

        self.assertEqual(0, server.connection_gate.in_flight())
        self.assertEqual(
            [
                mock.call("http_connections_in_flight", 1),
                mock.call("http_connections_in_flight", -1),
            ],
            gauge.call_args_list,
        )

    def test_rejects_before_spawning_and_releases_after_handler(self):
        server = _make_server(_settings(max_http_connections=1), mock.Mock())
        self.addCleanup(server.server_close)
        request = mock.Mock(spec=socket.socket)
        address = ("127.0.0.1", 1)

        self.assertTrue(server.connection_gate.try_acquire())
        with (
            mock.patch.object(api.ThreadingHTTPServer, "process_request") as spawn,
            mock.patch.object(server, "shutdown_request") as reject,
            mock.patch.object(api.metrics, "inc"),
        ):
            server.process_request(request, address)
        spawn.assert_not_called()
        reject.assert_called_once_with(request)

        with (
            mock.patch.object(api.ThreadingHTTPServer, "process_request_thread") as handle,
            mock.patch.object(api.metrics, "add_gauge"),
        ):
            server.process_request_thread(request, address)
        handle.assert_called_once_with(request, address)
        self.assertEqual(0, server.connection_gate.in_flight())


if __name__ == "__main__":
    unittest.main()
