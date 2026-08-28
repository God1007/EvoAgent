import http.client
import json
import runpy
import socket
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from unittest import mock

from evoagent import api
from evoagent.api import DRAINING, _make_server
from evoagent.auth import Principal
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

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
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

    def test_console_capabilities_and_fix_blockers_are_safe_read_only_hints(self):
        headers = {"X-EvoAgent-View": "console"}
        status, raw = self._get("/api/dashboard", headers)
        self.assertEqual(200, status)
        self.assertEqual(
            dict(
                role="platform_admin",
                review=True,
                manage=True,
                platform=True,
                github_install_configured=False,
            ),
            json.loads(raw)["capabilities"],
        )
        for role, review, manage, platform in (
            ("auditor", False, False, False),
            ("maintainer", True, False, False),
            ("admin", True, True, False),
            ("platform_admin", True, True, True),
        ):
            principal = Principal("user", "user", "default", role)
            caps = self.service.console_capabilities(principal)
            self.assertEqual(
                (review, manage, platform), tuple(caps[k] for k in ("review", "manage", "platform"))
            )
        original_settings = self.service.settings
        required = dict(
            auth_required=True,
            auth_secret="private-secret",
            github_app_slug="app",
            github_app_id="1",
            github_private_key_path="private-key-path",
            github_client_id="client",
            github_client_secret="private-client-secret",
            github_oauth_callback_url="https://example.invalid/callback",
            github_webhook_secret="private-webhook-secret",
        )
        self.service.settings = replace(original_settings, **required)
        self.assertTrue(self.service.console_capabilities(principal)["github_install_configured"])
        for key in required:
            with self.subTest(missing=key):
                self.service.settings = replace(
                    original_settings, **(required | {key: False if key == "auth_required" else ""})
                )
                self.assertFalse(
                    self.service.console_capabilities(principal)["github_install_configured"]
                )
        self.service.settings = original_settings
        principal = Principal("user", "user", "default", "maintainer")
        task = {
            "id": "a" * 32,
            "state": "SUCCESS",
            "repository": "demo/repo",
            "pull_request": 1,
            "report": {"findings": []},
            "input": {"head_sha": "private-head"},
        }

        def blocker():
            return self.service.console_fix_blocker(task, principal)

        self.service.policies.save("default", "demo/repo", {"auto_fix": False}, "test")
        self.assertEqual("policy", blocker())
        self.service.policies.save("default", "demo/repo", {"auto_fix": True}, "test")
        self.assertEqual("github", blocker())
        self.service.settings = replace(self.service.settings, github_token="private-token")
        self.assertEqual("sandbox", blocker())
        self.service.repair_container_image = "fixture-resolved-image"
        self.assertEqual("tests", blocker())
        self.service.settings = replace(self.service.settings, repair_test_command="pytest")
        self.assertEqual("", blocker())
        # No GitHub/model call or test process is launched by the hint endpoint.
        with (
            mock.patch.object(
                self.service.github, "get_pull_request", side_effect=AssertionError("external call")
            ),
            mock.patch.object(self.service.store, "get", return_value=task),
        ):
            status, raw = self._get("/v1/tasks/" + task["id"], headers)
            self.assertEqual(200, status)
            self.assertTrue(json.loads(raw)["can_fix"])
            self.assertNotIn(b"private-", raw)
        task["input"]["installation_id"] = True
        self.assertEqual("installation", blocker())
        task["input"] = {}
        self.assertEqual("pr_snapshot", blocker())
        self.assertEqual(
            "permission",
            self.service.console_fix_blocker(task, Principal("u", "u", "default", "auditor")),
        )
        self.assertEqual([], self.service.store.list_tasks())

    def test_unavailable_console_logging_does_not_break_http_responses(self):
        for failure in (
            BrokenPipeError(),
            OSError("output unavailable"),
            ValueError("closed stream"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(api, "print", side_effect=failure, create=True),
                mock.patch.object(api.metrics, "inc") as counter,
            ):
                for path, expected in (
                    ("/health", 200),
                    ("/", 200),
                    ("/assets/studio.js", 200),
                    ("/missing", 404),
                ):
                    status, body = self._get(path)
                    self.assertEqual(expected, status)
                    self.assertTrue(body)
                with mock.patch.object(
                    self.service, "readiness", side_effect=RuntimeError("private failure")
                ):
                    status, body = self._get("/ready")
                    self.assertEqual(500, status)
                    self.assertNotIn(b"private failure", body)
                counter.assert_any_call("http_log_failures_total")


class LifecycleTests(unittest.TestCase):
    def test_cli_help_and_invalid_arguments_never_start_the_service(self):
        for arguments, code in (
            (["--help"], 0),
            (["--dry-run"], 2),
            (["--port", "18081"], 2),
            (["unexpected"], 2),
        ):
            with (
                self.subTest(arguments=arguments),
                mock.patch("sys.argv", ["evoagent", *arguments]),
                mock.patch.object(
                    api.Settings, "from_env", side_effect=AssertionError("read configuration")
                ) as settings,
                mock.patch.object(api, "ReviewService") as service,
                mock.patch.object(api, "_make_server") as server,
                redirect_stdout(StringIO()),
                redirect_stderr(StringIO()),
            ):
                with self.assertRaises(SystemExit) as result:
                    runpy.run_module("evoagent", run_name="__main__")
                self.assertEqual(code, result.exception.code)
                settings.assert_not_called()
                service.assert_not_called()
                server.assert_not_called()

    def test_cli_without_arguments_uses_the_normal_service_lifecycle(self):
        with mock.patch.object(api, "run") as serve:
            api.main([])
        serve.assert_called_once_with()
        with mock.patch("sys.argv", ["evoagent"]), mock.patch.object(api, "run") as serve:
            runpy.run_module("evoagent", run_name="__main__")
        serve.assert_called_once_with()

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
        workflow_factory = mock.Mock()
        contributions = (mock.Mock(),)

        with (
            mock.patch("sys.argv", ["custom-workflow", "--serve"]),
            mock.patch.object(api.Settings, "from_env", return_value=settings),
            mock.patch.object(api, "ReviewService", return_value=service) as service_type,
            mock.patch.object(api, "_make_server", return_value=server),
            mock.patch.object(api, "_print_banner"),
            mock.patch.object(api, "_install_drain_on_sigterm") as install_drain,
        ):
            api.run(workflow_factory=workflow_factory, reviewer_contributions=contributions)

        service_type.assert_called_once_with(
            settings, workflow_factory=workflow_factory, reviewer_contributions=contributions
        )
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
