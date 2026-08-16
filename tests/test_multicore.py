import http.client
import os
import signal
import socket
import tempfile
import threading
import time
import unittest

from evoagent import api
from evoagent.api import (
    DRAINING,
    ReuseportThreadingHTTPServer,
    _can_multiprocess,
    _make_server,
    _reap_children,
)
from evoagent.config import Settings
from evoagent.service import ReviewService


def _settings(**overrides) -> Settings:
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


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        DRAINING.clear()
        self.addCleanup(DRAINING.clear)
        self.service = ReviewService(_settings())
        self.addCleanup(self.service.close)
        self.server = _make_server(_settings(), self.service)
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


@unittest.skipUnless(hasattr(socket, "SO_REUSEPORT"), "SO_REUSEPORT unavailable")
class ReuseportTests(unittest.TestCase):
    def test_two_servers_share_one_port(self):
        # Bind the first server to discover a free port, then bind a SECOND
        # server to the SAME port - only possible with SO_REUSEPORT - proving the
        # mechanism the multi-process workers rely on.
        DRAINING.clear()
        self.addCleanup(DRAINING.clear)
        service_a = ReviewService(_settings())
        self.addCleanup(service_a.close)
        server_a = _make_server(_settings(), service_a)
        host, port = server_a.server_address

        service_b = ReviewService(_settings())
        self.addCleanup(service_b.close)
        server_b = _make_server(_settings(port=port), service_b)
        self.assertEqual(port, server_b.server_address[1])

        for server in (server_a, server_b):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        for _ in range(10):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read()
            self.assertEqual(200, response.status)
            conn.close()

    def test_reuseport_server_is_threading(self):
        self.assertTrue(issubclass(ReuseportThreadingHTTPServer, api.ThreadingHTTPServer))


@unittest.skipUnless(hasattr(os, "fork"), "fork required")
class ReaperTests(unittest.TestCase):
    def test_reap_children_sigkills_stragglers_within_deadline(self):
        # A child that ignores SIGTERM must still be killed so the master never
        # hangs forever waiting on a stuck worker.
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(30)
            os._exit(0)
        children = {pid}
        started = time.monotonic()
        _reap_children(children, deadline_seconds=1.0)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(set(), children)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, 0)


class MultiprocessDecisionTests(unittest.TestCase):
    def test_single_worker_stays_single_process(self):
        self.assertFalse(_can_multiprocess(_settings(web_workers=1)))

    def test_multiple_workers_enable_forking_on_posix(self):
        expected = (
            _settings(web_workers=2).web_workers > 1
            and hasattr(__import__("os"), "fork")
            and hasattr(socket, "SO_REUSEPORT")
        )
        self.assertEqual(expected, _can_multiprocess(_settings(web_workers=2)))


if __name__ == "__main__":
    unittest.main()
